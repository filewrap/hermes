"""Safe Telegram managed-bot roster, peer allowlist, and bounded inbox operations.

This module deliberately never invokes a model turn for an incoming bot. Messages
from approved bots are retained in a profile-scoped inbox for explicit owner review.
The connected Telethon client is authenticated as Hermes's bot, not the owner's user
account, so user-only BotFather management requests are reported as unsupported.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

_SCHEMA = 1
_MAX_INBOX = 100
_MAX_MESSAGE = 4096
_MAX_BOT_MESSAGES_PER_MINUTE = 8
_MIN_BOT_MESSAGE_INTERVAL = 1.0
_TTL_SECONDS = 7 * 24 * 60 * 60
_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")

# Public descriptions consumed by the parent capabilities tool. Keep these aligned
# with TelegramBotOperations.request's canonical dot-separated action names.
BOT_OPERATION_PARAMETERS: dict[str, dict[str, str]] = {
    "capabilities": {},
    "managed.list": {},
    "managed.create_link": {
        "username": "required suggested Telegram bot username, 5–32 characters, letters/digits/underscore, ending in bot",
        "name": "optional suggested display name, 1–64 characters",
    },
    "managed.access.get": {"bot_id": "required positive Telegram bot ID"},
    "managed.access.set": {
        "bot_id": "required verified managed-bot ID",
        "restricted": "required boolean",
        "add_users": "required list of up to 10 positive human Telegram user IDs; empty list clears additions",
    },
    "managed.set_info": {"bot_id": "required managed-bot ID", "text": "unsupported; child profile text edits are not exposed"},
    "peers.list": {},
    "inbox.read": {
        "bot_id": "optional bot ID filter",
        "limit": "optional integer, clamped to 1–50 (default 20)",
    },
    "peer.add": {
        "bot_id": "required positive Telegram bot ID or exact @bot_username; Telegram verifies it is a bot before confirmation",
        "group_enabled": "required boolean; group grant is restricted to the current group/forum",
        "dm_enabled": "required boolean; independently enables direct messages with that bot",
    },
    "peer.remove": {"bot_id": "required allowlisted bot ID"},
    "peer.set_permissions": {
        "bot_id": "required allowlisted bot ID",
        "group_enabled": "required boolean; in groups applies only to current group; in DM must be false and leaves group grants unchanged",
        "dm_enabled": "required boolean; independently controls bot DM permission",
        "group_chat_id": "optional exact current group/forum ID; forbidden for DM-only updates",
    },
    "peer.send": {
        "bot_id": "required allowlisted bot ID",
        "text": "required message text, 1–4096 characters",
        "scope": "optional dm or group; defaults to current DM/group source chat",
        "group_chat_id": "optional exact current group/forum ID; required only when not inferable from source",
        "reply_to": "optional recent inbox message ID from this bot in the resolved destination chat",
    },
}


class BotOperationsError(ValueError):
    """A bot-management operation is invalid or unauthorized."""


def _new_document() -> dict[str, Any]:
    return {"schema": _SCHEMA, "peers": {}, "managed_bots": {}, "inbox": [], "sends": []}


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return _new_document()
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise BotOperationsError("The managed-bot profile store is invalid; refusing to change it")
    for key, default in (("peers", {}), ("managed_bots", {}), ("inbox", []), ("sends", [])):
        if not isinstance(data.get(key), type(default)):
            raise BotOperationsError("The managed-bot profile store is invalid; refusing to change it")
    return data


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class BotOperationsStore:
    """Atomic, no-cache store rooted in the resolved Hermes profile home."""

    def __init__(self, profile_home: str | Path):
        home = Path(profile_home).expanduser().resolve()
        self.path = home / "telegram" / "managed_bot_operations.json"

    def _mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with _locked(self.path):
            data = _load(self.path)
            result = fn(data)
            _atomic_json(self.path, data)
            return result

    def _read(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        with _locked(self.path):
            return fn(_load(self.path))

    def peers(self) -> list[dict[str, Any]]:
        return self._read(lambda data: [dict(v) for _, v in sorted(data["peers"].items())])

    def peer(self, bot_id: int | str) -> dict[str, Any] | None:
        return self._read(lambda data: (dict(data["peers"].get(str(bot_id)))
                                       if data["peers"].get(str(bot_id)) else None))

    def add_peer(self, bot: dict[str, Any], group_enabled: bool, dm_enabled: bool,
                 group_chat_id: str | int | None = None) -> dict[str, Any]:
        bot_id = str(bot["id"])
        if not _ID_RE.fullmatch(bot_id) or not (group_enabled or dm_enabled):
            raise BotOperationsError("Select at least one explicit chat permission")
        if group_enabled and (group_chat_id is None or not str(group_chat_id).lstrip("-").isdecimal()):
            raise BotOperationsError("Group permission must name one exact Telegram chat ID")
        entry = {"id": int(bot_id), "username": str(bot.get("username") or ""),
                 "name": str(bot.get("name") or "")[:80],
                 "group_chat_ids": [str(group_chat_id)] if group_enabled else [],
                 "dm_enabled": bool(dm_enabled),
                 "added_at": int(time.time())}
        def update(data):
            data["peers"][bot_id] = entry
            return dict(entry)
        return self._mutate(update)

    def remove_peer(self, bot_id: int | str) -> bool:
        return self._mutate(lambda data: data["peers"].pop(str(bot_id), None) is not None)

    def set_permissions(self, bot_id: int | str, *, group_enabled: bool, dm_enabled: bool,
                        group_chat_id: str | int | None = None) -> dict[str, Any]:
        def update(data):
            entry = data["peers"].get(str(bot_id))
            if not entry:
                raise BotOperationsError("That bot is not on this profile's allowlist")
            # A grant covers only the explicitly named chat; it never leaks to other groups.
            group_ids = set(map(str, entry.get("group_chat_ids", [])))
            if group_enabled:
                group_ids.add(str(group_chat_id))
            elif group_chat_id is not None:
                group_ids.discard(str(group_chat_id))
            entry["group_chat_ids"] = sorted(group_ids)
            entry["dm_enabled"] = bool(dm_enabled)
            return dict(entry)
        return self._mutate(update)

    def is_admitted(self, bot_id: int | str, scope: str, chat_id: str | int) -> bool:
        if scope in {"dm", "private", "direct"}:
            field = "dm_enabled"
        elif scope in {"group", "forum"}:
            field = "group_chat_ids"
        else:
            return False
        entry = self.peer(bot_id)
        return bool(entry and (entry.get(field) is True if field == "dm_enabled"
                               else str(chat_id) in set(map(str, entry.get(field, [])))))

    def reserve_send(self, bot_id: int | str, chat_id: str | int, text: str, reply_to: Any) -> bool:
        now = time.time()
        digest = hashlib.sha256(f"{bot_id}:{chat_id}:{reply_to}:{text}".encode()).hexdigest()
        def update(data):
            sends = [item for item in data["sends"] if now - float(item.get("at", now)) < 600]
            recent = [item for item in sends if str(item.get("bot_id")) == str(bot_id)
                      and str(item.get("chat_id")) == str(chat_id) and now - float(item.get("at", 0)) < 60]
            if len(recent) >= _MAX_BOT_MESSAGES_PER_MINUTE or any(item.get("digest") == digest for item in recent):
                data["sends"] = sends
                return False
            sends.append({"bot_id": str(bot_id), "chat_id": str(chat_id), "digest": digest, "at": now})
            data["sends"] = sends[-200:]
            return True
        return self._mutate(update)

    def managed_bots(self) -> list[dict[str, Any]]:
        return self._read(lambda data: [dict(v) for _, v in sorted(data["managed_bots"].items())])

    def record_managed_bot(self, bot: dict[str, Any]) -> None:
        bot_id = str(bot["id"])
        entry = {"id": int(bot_id), "username": str(bot.get("username") or ""),
                 "name": str(bot.get("name") or "")[:80], "observed_at": int(time.time())}
        self._mutate(lambda data: data["managed_bots"].__setitem__(bot_id, entry))

    def push_inbox(self, record: dict[str, Any]) -> tuple[bool, str]:
        now = time.time()
        bot_id = str(record["bot_id"])
        dedupe = hashlib.sha256(f"{bot_id}:{record['chat_id']}:{record['message_id']}".encode()).hexdigest()
        def update(data):
            inbox = [item for item in data["inbox"]
                     if now - float(item.get("received_at", now)) < _TTL_SECONDS]
            if any(item.get("dedupe") == dedupe for item in inbox):
                data["inbox"] = inbox
                return False, "duplicate"
            recent = [float(item.get("received_at", 0)) for item in inbox
                      if str(item.get("bot_id")) == bot_id and now - float(item.get("received_at", 0)) < 60]
            if len(recent) >= _MAX_BOT_MESSAGES_PER_MINUTE:
                data["inbox"] = inbox
                return False, "rate limited"
            if recent and now - max(recent) < _MIN_BOT_MESSAGE_INTERVAL:
                data["inbox"] = inbox
                return False, "cooldown"
            clean = dict(record)
            clean["dedupe"] = dedupe
            clean["received_at"] = now
            inbox.append(clean)
            data["inbox"] = inbox[-_MAX_INBOX:]
            return True, "queued for owner review"
        return self._mutate(update)

    def inbox(self, *, bot_id: int | str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        amount = max(1, min(int(limit), 50))
        def select(data):
            items = data["inbox"]
            if bot_id is not None:
                items = [item for item in items if str(item.get("bot_id")) == str(bot_id)]
            return [dict(item) for item in items[-amount:]]
        return self._read(select)


def managed_bot_capability() -> dict[str, Any]:
    """Report manager-bot methods from the installed schema; never probe user-only methods."""
    try:
        from telethon.tl import functions, types
    except ImportError:
        return {"available": False, "reason": "Telethon is not installed"}
    mtproto_bots = getattr(functions, "bots", None)
    methods = tuple(name for name in ("GetAccessSettingsRequest", "EditAccessSettingsRequest",
                                      "ExportBotTokenRequest")
                    if mtproto_bots is not None and hasattr(mtproto_bots, name))
    has_update = hasattr(types, "UpdateManagedBot")
    return {
        "available": bool({"GetAccessSettingsRequest", "EditAccessSettingsRequest"}.issubset(methods)),
        "schema_methods": methods,
        "update_type": "UpdateManagedBot" if has_update else None,
        "reason": ("Managed-child roster is learned from Telegram updates sent to this manager bot "
                   "when a child is created or updated. A full roster listing is not available "
                   "to a bot session. The manager-bot MTProto API supports access restrictions; "
                   "no Bot API or owner-user-session fallback is used."),
    }


def _format_api_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name in {"BotMethodInvalidError", "MethodInvalidError", "BotInvalidError", "UserBotInvalidError"}:
        return "Telegram does not support this managed-bot operation for the connected bot session."
    return f"Telegram request failed ({name}); success is unconfirmed. Check Telegram before retrying a write."


_USERNAME_SLUG_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def _generate_username_slug(length: int = 16) -> str:
    """Generate a base32-ish slug for Telegram username correlation.

    Sixteen characters from a 32-symbol alphabet gives 80 bits of entropy while
    keeping ``hermes_<slug>_bot`` under Telegram's 32-character username limit.
    """
    import secrets

    return "".join(secrets.choice(_USERNAME_SLUG_ALPHABET) for _ in range(length))


def _managed_create_deep_link(
    manager_bot: str,
    suggested_username: str | None = None,
    suggested_name: str | None = None,
) -> str:
    """Build a ``t.me/newbot`` deep link for managed bot creation.

    Lives here, next to its only caller: the ``t.me/newbot`` link is part of the
    managed-bot creation contract, not of the setup/pairing flow in
    ``hermes_cli.telegram_managed_bot`` (whose plugin-compat copy of this builder
    the upstream canary removed — the merge dropped it while this module still
    imported it).
    """
    import urllib.parse

    manager = manager_bot.lstrip("@")
    username = suggested_username or f"hermes_{_generate_username_slug()}_bot"
    base_url = (
        "https://t.me/newbot/"
        f"{urllib.parse.quote(manager)}/"
        f"{urllib.parse.quote(username)}"
    )
    if suggested_name:
        params = urllib.parse.urlencode({"name": suggested_name})
        return f"{base_url}?{params}"
    return base_url


_TELEGRAM_TOKEN_RE = re.compile(r"(?<![0-9])[0-9]{5,}:[A-Za-z0-9_-]{20,}")


def _redact_external_text(value: Any) -> str:
    """Scrub untrusted bot text before it is stored or shown to the owner."""
    text = str(value or "")
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:
        # Keep the explicit Telegram credential defense even if the shared redactor
        # cannot be imported in a minimal installation.
        pass
    return _TELEGRAM_TOKEN_RE.sub("[REDACTED TELEGRAM BOT TOKEN]", text)


class TelegramBotOperations:
    """Profile-isolated bot admission, read-only roster/inbox, and safe owner API."""

    def __init__(self, adapter: Any):
        self.adapter = adapter

    def profile_home(self, source: Any) -> str:
        runner = getattr(self.adapter, "gateway_runner", None)
        route = getattr(runner, "_resolve_profile_home_for_source", None)
        home = route(source) if callable(route) else None
        if home is None:
            resolver = getattr(self.adapter, "_settings_home", None)
            home = resolver(source) if callable(resolver) else None
        if home is None:
            raise BotOperationsError("Profile routing is unavailable; the operation was refused")
        return str(Path(home).expanduser().resolve())

    def _store(self, source: Any) -> BotOperationsStore:
        return BotOperationsStore(self.profile_home(source))

    def _is_owner(self, source: Any) -> bool:
        if (getattr(source, "is_bot", False) or getattr(source, "is_guest", False)
                or getattr(source, "profile_route_rejected", False)
                or not _ID_RE.fullmatch(str(getattr(source, "user_id", "")))):
            return False
        if str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct", "group", "forum"}:
            return False
        owner_check = getattr(self.adapter, "_is_owner_user", None)
        authorized = getattr(self.adapter, "_authorized", None)
        try:
            return bool(self.profile_home(source) and callable(owner_check) and owner_check(source.user_id, source)
                        and callable(authorized) and authorized(source))
        except Exception:
            return False

    def admission_check(self, source: Any) -> bool:
        """Fail-closed hook for the parent's incoming-bot branch; never admits humans."""
        if not bool(getattr(source, "is_bot", False)):
            return False
        bot_id = getattr(source, "user_id", None)
        if isinstance(bot_id, bool) or not str(bot_id or "").isdecimal():
            return False
        try:
            return self._store(source).is_admitted(bot_id, str(getattr(source, "chat_type", "")),
                                                  str(getattr(source, "chat_id", "")))
        except (BotOperationsError, OSError, ValueError):
            return False

    async def handle_incoming(self, event: Any, source: Any) -> bool:
        """Enqueue an approved bot message and report it handled; never start a turn."""
        if not self.admission_check(source):
            return False
        message = getattr(event, "message", None)
        text = _redact_external_text(getattr(message, "message", None) or getattr(event, "raw_text", "") or "")
        if not text.strip():
            return True
        text = text[:_MAX_MESSAGE]
        message_id = getattr(message, "id", None) or getattr(event, "id", None)
        if message_id is None:
            return True
        record = {
            "bot_id": int(source.user_id),
            "bot_username": str(getattr(source, "username", "") or "")[:64],
            "bot_name": str(getattr(source, "display_name", "") or "")[:80],
            "chat_id": str(source.chat_id),
            "chat_type": str(source.chat_type),
            "thread_id": str(getattr(source, "thread_id", "") or "") or None,
            "message_id": str(message_id),
            "text": text,
        }
        try:
            _accepted, _reason = self._store(source).push_inbox(record)
        except (BotOperationsError, OSError, ValueError):
            return True
        return True

    async def record_managed_update(self, source: Any, update: Any) -> dict[str, Any]:
        """Persist a verified UpdateManagedBot roster hint from an owner-authorized context."""
        if not self._is_owner(source):
            return {"ok": False, "error": "Owner authorization is required"}
        try:
            from telethon.tl import types
            if not isinstance(update, types.UpdateManagedBot):
                return {"ok": False, "error": "Unsupported managed-bot update type"}
            if str(update.user_id) != str(source.user_id):
                return {"ok": False, "error": "Update owner does not match this profile owner"}
            entity = await self.adapter._client.get_entity(int(update.bot_id))
            if not bool(getattr(entity, "bot", False)):
                return {"ok": False, "error": "Telegram did not identify that managed peer as a bot"}
            self._store(source).record_managed_bot({"id": int(entity.id),
                "username": getattr(entity, "username", None),
                "name": " ".join(filter(None, (getattr(entity, "first_name", ""), getattr(entity, "last_name", ""))))})
            return {"ok": True, "bot_id": int(entity.id)}
        except Exception as exc:
            return {"ok": False, "error": _format_api_error(exc)}

    async def handle_managed_update(self, update: Any, profile_home: str | Path | None = None) -> dict[str, Any]:
        """Record a Telegram-delivered UpdateManagedBot from the active manager-bot session."""
        try:
            from telethon.tl import types
            if not isinstance(update, types.UpdateManagedBot):
                return {"ok": False, "error": "Unsupported managed-bot update type"}
            if profile_home is None:
                from hermes_constants import get_hermes_home
                profile_home = get_hermes_home()
            if profile_home is None:
                return {"ok": False, "error": "Profile routing is unavailable"}
            entity = await self.adapter._client.get_entity(int(update.bot_id))
            if not bool(getattr(entity, "bot", False)):
                return {"ok": False, "error": "Telegram did not identify the managed peer as a bot"}
            BotOperationsStore(profile_home).record_managed_bot({
                "id": int(entity.id), "username": getattr(entity, "username", None),
                "name": " ".join(filter(None, (getattr(entity, "first_name", ""),
                                                  getattr(entity, "last_name", ""))))})
            return {"ok": True, "bot_id": int(entity.id)}
        except Exception as exc:
            return {"ok": False, "error": _format_api_error(exc)}

    async def request(self, source: Any, action: str, **params: Any) -> dict[str, Any]:
        """Owner-only source API for parent tool integration; mutations return a confirmation ticket."""
        if not self._is_owner(source):
            return {"ok": False, "error": "Owner authorization is required"}
        store = self._store(source)
        if action == "capabilities":
            return {"ok": True, **managed_bot_capability()}
        if action == "managed.list":
            return {"ok": True, "bots": store.managed_bots(), "capability": managed_bot_capability()}
        if action == "managed.create_link":
            if not bool(getattr(self.adapter, "_bot_can_manage_bots", False)):
                return {"ok": False, "error": "Managed-bot creation links require a manager-mode Telegram bot session"}
            username = str(params.get("username", ""))
            name = params.get("name")
            if (not 5 <= len(username) <= 32
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*bot", username, flags=re.IGNORECASE)):
                return {"ok": False, "error": "Suggested username must be 5–32 letters, digits, or underscores and end in bot"}
            if name is not None:
                name = str(name)
                if not 1 <= len(name) <= 64:
                    return {"ok": False, "error": "Suggested display name must contain 1–64 characters"}
            manager = str(getattr(self.adapter, "_bot_username", "") or "").lstrip("@")
            if not manager:
                return {"ok": False, "error": "Manager-mode bot username is unavailable; no creation link was generated"}
            try:
                url = _managed_create_deep_link(manager, username, name)
            except Exception as exc:
                return {"ok": False, "error": _format_api_error(exc)}
            return {"ok": True, "operation": "managed.create_link", "url": url,
                    "username": username, "name": name,
                    "message": "Open this Telegram link to continue in Telegram; Telegram handles creation and its own confirmation."}
        if action == "managed.set_info":
            return {"ok": False, "error": "Child profile text edits are not exposed by this safe manager-bot API. "
                    "Supported configuration is managed-bot access settings."}
        if action == "managed.access.get":
            bot_id = params.get("bot_id")
            if bot_id is None or not any(str(item["id"]) == str(bot_id) for item in store.managed_bots()):
                return {"ok": False, "error": "Bot is not in this profile's verified managed-bot roster"}
            try:
                from telethon.tl import functions
                entity = await self.adapter._client.get_entity(int(bot_id))
                if not bool(getattr(entity, "bot", False)):
                    return {"ok": False, "error": "Telegram no longer identifies this peer as a bot"}
                settings = await self.adapter._client(functions.bots.GetAccessSettingsRequest(
                    bot=await self.adapter._client.get_input_entity(entity)))
                users = list(getattr(settings, "add_users", None) or [])
                return {"ok": True, "bot_id": int(bot_id),
                        "restricted": bool(getattr(settings, "restricted", False)),
                        "add_users": [{"id": int(user.id), "username": getattr(user, "username", None),
                                       "name": " ".join(filter(None, (getattr(user, "first_name", ""),
                                                                         getattr(user, "last_name", ""))))}
                                      for user in users]}
            except Exception as exc:
                return {"ok": False, "error": _format_api_error(exc)}
        if action == "managed.access.set":
            bot_id = str(params.get("bot_id", ""))
            if not any(str(item["id"]) == bot_id for item in store.managed_bots()):
                return {"ok": False, "error": "Bot is not in this profile's verified managed-bot roster"}
            restricted, users = params.get("restricted"), params.get("add_users")
            if type(restricted) is not bool or not isinstance(users, list) or len(users) > 10:
                return {"ok": False, "error": "Pass restricted as a boolean and add_users as an explicit list of at most 10 user IDs"}
            if users and not restricted:
                return {"ok": False, "error": "Telegram only accepts additional users while restricted access is enabled"}
            clean_users = []
            for user_id in users:
                if isinstance(user_id, bool) or not str(user_id).isdecimal() or int(user_id) <= 0:
                    return {"ok": False, "error": "add_users must contain only positive Telegram user IDs"}
                clean_users.append(int(user_id))
            if len(set(clean_users)) != len(clean_users):
                return {"ok": False, "error": "add_users contains duplicate IDs"}
            return {"ok": True, "confirmation_required": True, "operation": "managed.access.set",
                    "bot_id": bot_id, "restricted": restricted, "add_users": clean_users}
        if action == "peers.list":
            return {"ok": True, "peers": store.peers()}
        if action == "inbox.read":
            return {"ok": True, "messages": store.inbox(bot_id=params.get("bot_id"), limit=params.get("limit", 20))}
        if action == "peer.add":
            bot_id = params.get("bot_id")
            raw_id = str(bot_id or "")
            if isinstance(bot_id, bool) or not (raw_id.isdecimal() or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", raw_id)):
                return {"ok": False, "error": "Specify a positive Telegram bot ID or exact @bot_username"}
            if type(params.get("group_enabled")) is not bool or type(params.get("dm_enabled")) is not bool:
                return {"ok": False, "error": "Pass both group_enabled and dm_enabled as explicit booleans"}
            try:
                entity = await self.adapter._client.get_entity(int(raw_id) if raw_id.isdecimal() else raw_id)
            except Exception:
                return {"ok": False, "error": "Telegram could not resolve that peer; nothing was added"}
            if not bool(getattr(entity, "bot", False)):
                return {"ok": False, "error": "Peer is not a Telegram bot; nothing was added"}
            if store.peer(entity.id):
                return {"ok": False, "error": "This bot is already approved; use peer.set_permissions for explicit scope changes"}
            group = params.get("group_enabled") is True
            dm = params.get("dm_enabled") is True
            if not (group or dm):
                return {"ok": False, "error": "Set group_enabled and/or dm_enabled explicitly"}
            group_chat_id = str(getattr(source, "chat_id", "")) if group else None
            if group and str(getattr(source, "chat_type", "")) not in {"group", "forum"}:
                return {"ok": False, "error": "Group access can only be granted for the exact current group or forum chat"}
            bot = {"id": int(entity.id), "username": getattr(entity, "username", None),
                   "name": " ".join(filter(None, (getattr(entity, "first_name", ""), getattr(entity, "last_name", ""))))}
            return {"ok": True, "confirmation_required": True, "operation": "peer.add",
                    "bot": bot, "group_enabled": group, "dm_enabled": dm,
                    "group_chat_id": group_chat_id}
        if action == "peer.remove":
            bot_id = params.get("bot_id")
            if not store.peer(bot_id) if bot_id is not None else True:
                return {"ok": False, "error": "That bot is not on this profile's allowlist"}
            return {"ok": True, "confirmation_required": True, "operation": "peer.remove", "bot_id": str(bot_id)}
        if action == "peer.set_permissions":
            bot_id = str(params.get("bot_id", ""))
            existing = store.peer(bot_id)
            if not existing:
                return {"ok": False, "error": "That bot is not on this profile's allowlist"}
            if "group_enabled" not in params or "dm_enabled" not in params:
                return {"ok": False, "error": "Pass both group_enabled and dm_enabled explicitly"}
            group, dm = params["group_enabled"], params["dm_enabled"]
            if type(group) is not bool or type(dm) is not bool:
                return {"ok": False, "error": "Use explicit booleans for group_enabled and dm_enabled"}
            group_chat_id = params.get("group_chat_id")
            chat_type = str(getattr(source, "chat_type", ""))
            if chat_type in {"dm", "private", "direct"}:
                if group or group_chat_id is not None:
                    return {"ok": False, "error": "In a DM only DM permission may change; leave group_enabled=false and omit group_chat_id"}
                return {"ok": True, "confirmation_required": True, "operation": "peer.set_permissions",
                        "bot_id": bot_id, "group_enabled": False, "dm_enabled": dm, "group_chat_id": None}
            if group_chat_id is None and chat_type in {"group", "forum"}:
                group_chat_id = str(source.chat_id)
            if group_chat_id is None:
                return {"ok": False, "error": "Pass the exact group_chat_id whose permission is being changed"}
            if (str(getattr(source, "chat_type", "")) not in {"group", "forum"}
                    or str(group_chat_id) != str(source.chat_id)):
                return {"ok": False, "error": "Group permissions can only be changed for the exact current group or forum chat"}
            return {"ok": True, "confirmation_required": True, "operation": "peer.set_permissions",
                    "bot_id": bot_id, "group_enabled": group, "dm_enabled": dm,
                    "group_chat_id": str(group_chat_id)}
        if action == "peer.send":
            bot_id, text = params.get("bot_id"), str(params.get("text", ""))
            peer = store.peer(bot_id) if bot_id is not None else None
            if not peer:
                return {"ok": False, "error": "Sending requires an approved bot peer"}
            if not text.strip() or len(text) > _MAX_MESSAGE:
                return {"ok": False, "error": f"Message must be 1–{_MAX_MESSAGE} characters"}
            current_type = str(getattr(source, "chat_type", ""))
            scope = str(params.get("scope") or (
                "dm" if current_type in {"dm", "private", "direct"}
                else "group" if current_type in {"group", "forum"} else ""))
            if scope in {"dm", "private", "direct"}:
                if not peer.get("dm_enabled"):
                    return {"ok": False, "error": "This peer's direct-message permission is disabled"}
                target_chat_id = str(peer["id"])
            elif scope in {"group", "forum"}:
                chat_id = str(params.get("group_chat_id") or source.chat_id)
                if (str(getattr(source, "chat_type", "")) not in {"group", "forum"}
                        or chat_id != str(source.chat_id)):
                    return {"ok": False, "error": "Group sends must target the exact current group or forum chat"}
                if chat_id not in set(map(str, peer.get("group_chat_ids", []))):
                    return {"ok": False, "error": "This exact group is not approved for the peer"}
                target_chat_id = chat_id
            else:
                return {"ok": False, "error": "Specify scope=dm or scope=group and the exact group chat_id"}
            reply_to = params.get("reply_to")
            thread_id = (str(getattr(source, "thread_id", "") or "")
                         if scope in {"group", "forum"} else "")
            if reply_to is not None and not any(str(item.get("message_id")) == str(reply_to)
                    and str(item.get("bot_id")) == str(bot_id)
                    and str(item.get("chat_id")) == target_chat_id
                    and str(item.get("thread_id") or "") == thread_id for item in store.inbox(limit=50)):
                return {"ok": False, "error": "Replies may only target a recent inbox message from this approved bot in the same chat"}
            if scope in {"group", "forum"}:
                username = str(peer.get("username") or "").lstrip("@")
                if not username:
                    return {"ok": False, "error": "A verified public bot username is required for a group mention"}
                # The full, final message is previewed BEFORE owner approval.
                if reply_to is None and not text.lower().startswith(f"@{username.lower()} "):
                    text = f"@{username} {text}"
                if len(text) > _MAX_MESSAGE:
                    return {"ok": False, "error": "Message plus required bot mention exceeds Telegram's text limit"}
            return {"ok": True, "confirmation_required": True, "operation": "peer.send",
                    "bot_id": str(bot_id), "text": text, "scope": scope,
                    "chat_id": target_chat_id, "thread_id": thread_id,
                    "reply_to": reply_to}
        return {"ok": False, "error": "Unknown bot operation"}

    async def confirm(self, source: Any, operation: str, **payload: Any) -> dict[str, Any]:
        """Apply one exact action after the caller's fresh explicit owner confirmation."""
        if not self._is_owner(source):
            return {"ok": False, "error": "Owner authorization is required"}
        store = self._store(source)
        try:
            if operation == "peer.add":
                bot = payload.get("bot")
                if not isinstance(bot, dict):
                    return {"ok": False, "error": "Invalid confirmation payload"}
                entity = await self.adapter._client.get_entity(int(bot["id"]))
                if not bool(getattr(entity, "bot", False)) or int(entity.id) != int(bot["id"]):
                    return {"ok": False, "error": "Peer identity changed; nothing was added"}
                if (not self._is_owner(source) or Path(self.profile_home(source)) != store.path.parent.parent
                        or store.peer(bot["id"])):
                    return {"ok": False, "error": "Owner/profile or peer state changed; request a new confirmation"}
                stored = store.add_peer(bot, payload.get("group_enabled") is True,
                                        payload.get("dm_enabled") is True,
                                        payload.get("group_chat_id"))
                return {"ok": True, "peer": stored}
            if operation == "peer.remove":
                if store.remove_peer(payload.get("bot_id")):
                    return {"ok": True, "removed": True}
                return {"ok": False, "error": "Peer was already absent"}
            if operation == "peer.set_permissions":
                stored = store.set_permissions(payload.get("bot_id"),
                    group_enabled=payload.get("group_enabled") is True,
                    dm_enabled=payload.get("dm_enabled") is True,
                    group_chat_id=payload.get("group_chat_id"))
                return {"ok": True, "peer": stored}
            if operation == "peer.send":
                bot_id, text = str(payload.get("bot_id", "")), str(payload.get("text", ""))
                peer = store.peer(bot_id)
                if not peer:
                    return {"ok": False, "error": "Approved peer is no longer on this profile's allowlist"}
                scope, chat_id = str(payload.get("scope", "")), str(payload.get("chat_id", ""))
                if scope in {"dm", "private", "direct"}:
                    if not peer.get("dm_enabled") or chat_id != bot_id:
                        return {"ok": False, "error": "Direct-message permission or target mismatch"}
                    destination = await self.adapter._client.get_entity(int(bot_id))
                elif scope in {"group", "forum"}:
                    if (str(getattr(source, "chat_type", "")) not in {"group", "forum"}
                            or chat_id != str(source.chat_id)
                            or chat_id not in set(map(str, peer.get("group_chat_ids", [])))):
                        return {"ok": False, "error": "This exact group is no longer approved"}
                    destination = await self.adapter._client.get_entity(int(chat_id))
                    username = str(peer.get("username") or "").lstrip("@")
                    if not username:
                        return {"ok": False, "error": "A public bot username is required for a visible group mention"}
                    if str(payload.get("thread_id") or "") != str(getattr(source, "thread_id", "") or ""):
                        return {"ok": False, "error": "The confirmed forum topic no longer matches"}
                else:
                    return {"ok": False, "error": "Invalid confirmed send scope"}
                entity = await self.adapter._client.get_entity(int(bot_id))
                if not bool(getattr(entity, "bot", False)):
                    return {"ok": False, "error": "Telegram no longer identifies the target as a bot"}
                if scope in {"group", "forum"} and str(getattr(entity, "username", "") or "").lower() != username.lower():
                    return {"ok": False, "error": "The bot username changed; refresh and reapprove the peer before sending"}
                reply_to = payload.get("reply_to")
                if reply_to is not None and not any(str(item.get("message_id")) == str(reply_to)
                        and str(item.get("bot_id")) == bot_id and str(item.get("chat_id")) == chat_id
                        and str(item.get("thread_id") or "") == str(payload.get("thread_id") or "")
                        for item in store.inbox(limit=50)):
                    return {"ok": False, "error": "The referenced bot inbox message is no longer available"}
                if (not self._is_owner(source) or Path(self.profile_home(source)) != store.path.parent.parent
                        or not store.is_admitted(bot_id, scope, chat_id)):
                    return {"ok": False, "error": "Owner authorization, profile or peer permission changed; no send was attempted"}
                effective_reply = reply_to or (int(payload["thread_id"]) if payload.get("thread_id") else None)
                if not store.reserve_send(bot_id, chat_id, text, reply_to):
                    return {"ok": False, "error": "Duplicate or rate-limited bot send; no message was sent"}
                # _client is authenticated as the manager bot. Do not fall back to the owner's
                # user identity or HTTP Bot API; incoming bot messages are inbox-only, so this
                # confirmed single send cannot auto-trigger a bot-to-bot reply loop.
                result = await self.adapter._client.send_message(destination, text, reply_to=effective_reply,
                                                                 parse_mode=None)
                message_id = getattr(result, "id", None)
                if result is False or not isinstance(message_id, int) or message_id <= 0:
                    return {"ok": False, "error": "Telegram did not confirm the message send; check Telegram before retrying"}
                return {"ok": True, "message_id": message_id, "chat_id": chat_id,
                        "thread_id": payload.get("thread_id") or None, "bot_id": bot_id}
            if operation == "managed.access.set":
                bot_id = str(payload.get("bot_id", ""))
                if not any(str(item["id"]) == bot_id for item in store.managed_bots()):
                    return {"ok": False, "error": "Bot is no longer in this profile's verified managed-bot roster"}
                restricted, user_ids = payload.get("restricted"), payload.get("add_users")
                if type(restricted) is not bool or not isinstance(user_ids, list) or len(user_ids) > 10:
                    return {"ok": False, "error": "Invalid access-settings confirmation payload"}
                from telethon.tl import functions
                entity = await self.adapter._client.get_entity(int(bot_id))
                if not bool(getattr(entity, "bot", False)):
                    return {"ok": False, "error": "Telegram no longer identifies this peer as a bot"}
                user_entities = []
                for user_id in user_ids:
                    if isinstance(user_id, bool) or not str(user_id).isdecimal() or int(user_id) <= 0:
                        return {"ok": False, "error": "Invalid confirmed user ID"}
                    user = await self.adapter._client.get_entity(int(user_id))
                    if bool(getattr(user, "bot", False)):
                        return {"ok": False, "error": "Additional access entries must be human users"}
                    user_entities.append(await self.adapter._client.get_input_entity(user))
                if not self._is_owner(source) or Path(self.profile_home(source)) != store.path.parent.parent:
                    return {"ok": False, "error": "Owner authorization or profile changed; request a new confirmation"}
                result = await self.adapter._client(functions.bots.EditAccessSettingsRequest(
                    bot=await self.adapter._client.get_input_entity(entity),
                    restricted=restricted, add_users=user_entities))
                if result is False:
                    return {"ok": False, "error": "Telegram did not confirm the access-settings change"}
                return {"ok": True, "bot_id": int(bot_id), "result": result}
        except Exception as exc:
            return {"ok": False, "error": _format_api_error(exc)}
        return {"ok": False, "error": "This operation has no supported Telethon implementation"}

    async def list_managed_bots(self, source: Any) -> dict[str, Any]:
        """UI alias: return observed roster hints and exact transport limitations."""
        return await self.request(source, "managed.list")

    async def read_inbox(self, source: Any, *, bot_id: int | str | None = None, limit: int = 20) -> dict[str, Any]:
        return await self.request(source, "inbox.read", bot_id=bot_id, limit=limit)
