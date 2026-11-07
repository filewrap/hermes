"""Telethon-only transport for the private Telegram forum audit outbox.

The controller is deliberately adapter-local. It never downloads messages, never
blocks an inbound handler on delivery, and does not trust persisted verification
flags: forum type and bot permissions are probed against the live MTProto client.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_TOPIC_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_CONFIG_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_CONTROLLERS: weakref.WeakSet = weakref.WeakSet()


class _OperationsLogHandler(logging.Handler):
    """Forward only generic warning metadata from this profile's operational logs."""

    _PREFIXES = ("gateway.run", "gateway.run_adapters",
                 "plugins.platforms.telegram.telethon_adapter")

    def __init__(self, home: Path):
        super().__init__(level=logging.WARNING)
        self.home = home

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(self._PREFIXES):
            return
        if getattr(record, "hermes_profile_scoped", None) is False:
            return
        try:
            from hermes_constants import get_hermes_home_override
            value = getattr(record, "hermes_home", None) or get_hermes_home_override()
            if not value:
                return  # Ambient process home is not evidence of an event's profile.
            record_home = Path(value).expanduser().resolve(strict=False)
        except (TypeError, ValueError, OSError):
            return
        if record_home != self.home:
            return
        handled = getattr(record, "_telegram_audit_handled_profiles", None)
        if handled is None:
            handled = set()
            record._telegram_audit_handled_profiles = handled
        if str(self.home) in handled:
            return
        handled.add(str(self.home))
        from .forum_audit import enqueue_operation
        component = re.sub(r"[^A-Za-z0-9_.-]", "", record.name)[:80]
        # Deliberately omit getMessage(), args, exception data, and stack traces.
        enqueue_operation(self.home,
                          f"{record.levelname} {component}: operational warning; see this profile's redacted logs.")


def _stable_id(*parts: Any) -> int:
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8", "replace")
    return (int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") & ((1 << 63) - 1)) or 1


def _confirmed_topic_id(response: Any, *, random_id: int, peer: Any, title: str) -> int:
    from telethon import types, utils
    pending, seen = [response], set()
    mapped, created = set(), set()
    while pending:
        update = pending.pop()
        if update is None or id(update) in seen:
            continue
        seen.add(id(update))
        pending.extend(getattr(update, "updates", ()) or ())
        nested = getattr(update, "update", None)
        if nested is not None:
            pending.append(nested)
        if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
            mapped.add(update.id)
        message = getattr(update, "message", None)
        if isinstance(message, types.MessageService) and isinstance(message.action, types.MessageActionTopicCreate):
            if (utils.get_peer_id(message.peer_id) == utils.get_peer_id(peer)
                    and message.action.title == title):
                created.add(message.id)
    if len(mapped) != 1 or not mapped.issubset(created) or next(iter(mapped)) <= 1:
        raise RuntimeError("Telegram did not confirm this request's topic creation")
    return next(iter(mapped))


def _signed_chat_id(value: Any) -> int | None:
    if isinstance(value, bool) or not re.fullmatch(r"-[1-9][0-9]{1,18}", str(value or "").strip()):
        return None
    number = int(value)
    return number if abs(number) < (1 << 63) else None


def _setup_chat_id(value: Any) -> int | None:
    """Parse syntax only; positive IDs remain untrusted until Channel resolution."""
    if isinstance(value, bool) or not re.fullmatch(r"-?[1-9][0-9]{0,18}", str(value or "").strip()):
        return None
    number = int(value)
    if number > 0:
        return number if number < (1 << 63) - 10**12 else None
    from telethon import types, utils
    _, peer_type = utils.resolve_id(number)
    return number if abs(number) < (1 << 63) and peer_type is types.PeerChannel else None


def _text_chunks(text: str, limit: int) -> list[str]:
    """Split bounded audit notices into Telegram-safe UTF-16 message lengths."""
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        char_units = 2 if ord(char) > 0xFFFF else 1
        if current and units + char_units > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += char_units
    if current:
        chunks.append("".join(current))
    return chunks or [""]


class ForumAuditTransport:
    """Manage one adapter's forum audit service and lifecycle-bound drainer."""

    def __init__(self, adapter: Any, profile_home: str | Path, *,
                 parent: ForumAuditTransport | None = None):
        self.adapter = adapter
        self.home = Path(profile_home).expanduser().resolve(strict=False)
        self.profile_id = hashlib.sha256(str(self.home).encode()).hexdigest()[:12]
        self._manager = parent or self
        self._profiles: dict[Path, ForumAuditTransport] = {self.home: self}
        lock_key = str(self.home)
        self._topic_lock = _TOPIC_LOCKS.setdefault(lock_key, asyncio.Lock())
        self._configuration_lock = _CONFIG_LOCKS.setdefault(lock_key, asyncio.Lock())
        self._flood_until = 0.0
        self._revoked = False
        self._worker_generation = 0
        self._task: asyncio.Task | None = None
        self._service = None
        self._enabled_config: dict[str, Any] | None = None
        self._operations_handler: _OperationsLogHandler | None = None
        self._original_process = None
        self._original_connect = None
        self._original_disconnect = None
        if parent is None:
            self._install_hooks()
        self._new_service({})
        _CONTROLLERS.add(self)

    def _new_service(self, configuration: dict[str, Any] | None, *, runtime_verified: bool = False) -> None:
        from .forum_audit import ForumAuditLogger
        config = dict(configuration) if isinstance(configuration, dict) else {}
        # Persisted `verified_*` bits are not evidence. Service enablement is
        # withheld until this controller's live MTProto checks succeed.
        if not runtime_verified:
            config.update(enabled=False, verified_entity=False, verified_forum=False,
                          verified_rights=False)
        if self._service is not None:
            self._service.enabled = False
        transport_id = None if self is self._manager else str(self._manager.home)
        self._service = ForumAuditLogger(self.home, config, transport_id=transport_id)
        if self is self._manager:
            self.adapter._forum_audit = self._service  # Legacy primary-profile API.

    def _raw_config(self) -> dict[str, Any]:
        if self is not self._manager:
            from hermes_cli.config import read_user_config_raw
            try:
                raw = read_user_config_raw(self.home / "config.yaml")
                for key in ("platforms", "telegram", "extra", "forum_audit"):
                    raw = raw.get(key, {}) if isinstance(raw, dict) else {}
            except Exception:
                logger.warning("Telegram audit profile configuration unavailable; logging disabled")
                return {}
            return dict(raw) if isinstance(raw, dict) else {}
        extra = getattr(getattr(self.adapter, "config", None), "extra", {}) or {}
        raw = extra.get("forum_audit") if isinstance(extra, dict) else None
        return dict(raw) if isinstance(raw, dict) else {}

    def _trusted_homes(self) -> dict[str, Path]:
        runner = getattr(self.adapter, "gateway_runner", None)
        homes = getattr(runner, "_served_profile_homes", None)
        if not isinstance(homes, dict):
            return {}
        trusted = {}
        for name, home in homes.items():
            try:
                trusted[str(name)] = Path(home).expanduser().resolve(strict=False)
            except (TypeError, ValueError, OSError):
                logger.warning("Telegram audit served-profile entry invalid; entry excluded")
        return trusted

    def _profile_controller(self, home: Path) -> ForumAuditTransport | None:
        manager = self._manager
        if home not in manager._profiles and home in self._trusted_homes().values():
            manager._profiles[home] = ForumAuditTransport(self.adapter, home, parent=manager)
        return manager._profiles.get(home)

    @contextmanager
    def _policy_scope(self):
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(self.home):
            yield

    def _install_hooks(self) -> None:
        adapter = self.adapter
        self._original_connect = adapter.connect
        self._original_disconnect = adapter.disconnect
        self._original_process = adapter._process_inbound_event

        async def connect(*args, **kwargs):
            result = await self._original_connect(*args, **kwargs)
            if result:
                await self.connected()
            return result

        async def disconnect(*args, **kwargs):
            await self.disconnected()
            return await self._original_disconnect(*args, **kwargs)

        async def process_inbound(event, messages):
            # Audit admission is independent of group triggers: every non-DM
            # proceeds only to the original routing/reply path, never the outbox.
            try:
                source = await adapter._source(event)
                if self._is_audit_chat(getattr(source, "chat_id", None)):
                    # Audit forum messages never become model turns, including
                    # messages authored by an otherwise allowlisted user.
                    return
            except Exception:
                # Auditing must fail closed and must never disrupt the normal turn.
                logger.warning("Telegram forum audit inbound admission failed closed")
            return await self._original_process(event, messages)

        adapter.connect = connect
        adapter.disconnect = disconnect
        adapter._process_inbound_event = process_inbound

    def _is_audit_chat(self, chat_id: Any) -> bool:
        for home in set(self._trusted_homes().values()):
            self._profile_controller(home)
        for controller in self._manager._profiles.values():
            service = controller._service
            if service is not None and service.forum_id is not None and str(chat_id) == str(service.forum_id):
                return True
            raw = controller._raw_config()
            if _signed_chat_id(raw.get("chat_id")) is not None and str(chat_id) == str(raw.get("chat_id")):
                return True
        return False

    async def connected(self) -> None:
        for home in set(self._trusted_homes().values()):
            self._profile_controller(home)
        for controller in list(self._manager._profiles.values()):
            await controller._connect_profile()

    async def _connect_profile(self) -> None:
        async with self._configuration_lock:
            await self._connect_profile_locked()

    async def _connect_profile_locked(self) -> None:
        if self._revoked:
            return
        await self._stop_worker()
        if self._revoked:
            return
        epoch = self._worker_generation
        raw = self._raw_config()
        self._enabled_config = raw
        if raw.get("enabled") is not True:
            self._new_service(raw)
            return
        self._new_service(raw)  # Load this partition's persisted cooldown before any probe.
        try:
            chat_id = _setup_chat_id(raw.get("chat_id"))
            if chat_id is None:
                raise ValueError("forum chat ID must be a raw channel ID or canonical channel peer ID")
            chat_id, general, operations = await self._verify_and_prepare(chat_id)
            self._assert_live_epoch(epoch)
            verified = {"enabled": True, "chat_id": chat_id,
                        "general_topic_id": general, "operations_topic_id": operations,
                        "verified_entity": True, "verified_forum": True,
                        "verified_rights": True}
            self._persist_config(verified)
            self._new_service(verified, runtime_verified=True)
            self._set_operations_capture(True)
            self._start_worker()
            logger.info("Telegram private forum audit enabled for the active profile")
        except Exception as exc:
            self._set_operations_capture(False)
            self._new_service({})
            logger.warning("Telegram private forum audit disabled after live verification failed (%s)",
                           type(exc).__name__)

    async def disconnected(self) -> None:
        targets = list(self._profiles.values()) if self is self._manager else [self]
        for controller in targets:
            await controller._stop_worker()
            controller._set_operations_capture(False)
            controller._new_service(controller._raw_config())

    async def revoke_profile(self, home: str | Path) -> None:
        """Detach a removed profile before gateway teardown awaits or deletes its home."""
        home = Path(home).expanduser().resolve(strict=False)
        controller = self._manager._profiles.get(home)
        if controller is None:
            return
        controller._revoked = True
        controller._set_operations_capture(False)
        await controller._stop_worker()
        controller._new_service({})  # Inert; never persist or recreate a removed home's files.
        if controller is not self._manager:
            self._manager._profiles.pop(home, None)

    def _set_operations_capture(self, enabled: bool) -> None:
        if self._operations_handler is not None:
            for name in _OperationsLogHandler._PREFIXES:
                logging.getLogger(name).removeHandler(self._operations_handler)
            self._operations_handler.close()
            self._operations_handler = None
        if enabled:
            handler = _OperationsLogHandler(self.home)
            for name in _OperationsLogHandler._PREFIXES:
                logging.getLogger(name).addHandler(handler)
            self._operations_handler = handler

    async def _stop_worker(self) -> None:
        self._worker_generation += 1
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            done, pending = await asyncio.wait({task}, timeout=2.0)
            if pending:
                logger.warning("Telegram forum audit worker detached after bounded cancellation")
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            for finished in done:
                if not finished.cancelled():
                    finished.exception()

    def _start_worker(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain_loop(self._worker_generation),
                                             name="telegram-forum-audit-drainer")

    async def _verify_forum(self, chat_id: int) -> tuple[Any, Any]:
        until = self._cooldown()
        if until > time.time():
            error = RuntimeError("Telegram audit transport cooldown")
            error.seconds = max(1, until - time.time())
            raise error
        client = self.adapter._client
        if client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        from telethon import types, utils
        parsed = _setup_chat_id(chat_id)
        if parsed is None:
            raise ValueError("invalid forum channel ID")
        # An explicit PeerChannel avoids positive integers resolving as Telegram users.
        entity = await client.get_entity(types.PeerChannel(parsed) if parsed > 0 else parsed)
        if (not isinstance(entity, types.Channel)
                or (entity.id != parsed if parsed > 0 else utils.get_peer_id(entity) != parsed)
                or getattr(entity, "forum", None) is not True
                or getattr(entity, "username", None)
                or getattr(entity, "usernames", None)
                or not getattr(entity, "megagroup", False)):
            raise ValueError("configured destination is not a private forum")
        me = await client.get_me()
        permissions = await client.get_permissions(entity, me)
        participant = getattr(permissions, 'participant', None)
        rights = getattr(participant, 'admin_rights', None)
        # Telethon 1.45 ParticipantPermissions has neither send_messages nor
        # manage_topics properties. Forum admins may send; topic management
        # must be present on the actual participant's ChatAdminRights.
        creator = getattr(permissions, 'is_creator', False) is True
        admin = getattr(permissions, 'is_admin', False) is True
        banned = getattr(permissions, 'is_banned', False) is True
        left = getattr(permissions, 'has_left', False) is True
        if (getattr(me, "bot", False) is not True or banned or left
                or not (creator or (admin and getattr(rights, 'manage_topics', False) is True))):
            raise PermissionError("bot lacks Send Messages or Manage Topics rights")
        return entity, permissions

    async def _verify_topic(self, chat_id: int, topic_id: int, *, title: str | None = None) -> int:
        from telethon import functions, types
        peer = await self.adapter._input_peer_for(chat_id)
        response = await self._audit_request(
            functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=[topic_id]))
        matches = [item for item in (getattr(response, "topics", ()) or ())
                   if int(getattr(item, "id", 0)) == topic_id
                   and not isinstance(item, types.ForumTopicDeleted)
                   and hasattr(item, "title")
                   and not getattr(item, "closed", False) and not getattr(item, "hidden", False)
                   and (title is None or item.title == title)]
        if len(matches) != 1:
            raise RuntimeError("saved forum topic failed live by-ID verification")
        return topic_id

    def _topic_storage(self, chat_id: int) -> Path:
        from .forum_audit import _storage_root
        transport_id = None if self is self._manager else str(self._manager.home)
        return _storage_root(self.home, chat_id, transport_id)

    async def _ensure_topic(self, chat_id: int, key: str, title: str, *,
                            random_id: int, known_id: int | None = None) -> int:
        from .forum_audit import _db, _MAX_ROWS
        epoch = self._worker_generation
        self._assert_live_epoch(epoch)
        root = self._topic_storage(chat_id)
        with _db(root) as db:
            row = db.execute("SELECT random_id,topic_id FROM forum_topics WHERE topic_key=?", (key,)).fetchone()
            if row is None:
                if db.execute("SELECT count(*) FROM forum_topics").fetchone()[0] >= _MAX_ROWS:
                    raise RuntimeError("forum topic ledger is full")
                db.execute("INSERT INTO forum_topics VALUES(?,?,?)", (key, random_id, known_id))
                stored_id = known_id
            else:
                random_id, stored_id = row
                if known_id is not None and stored_id is not None and known_id != stored_id:
                    raise RuntimeError("conflicting saved forum topic IDs")
                if stored_id is None and known_id is not None:
                    stored_id = known_id
                    db.execute("UPDATE forum_topics SET topic_id=? WHERE topic_key=?", (known_id, key))
        if stored_id is None:
            stored_id = await self._create_topic(chat_id, title, random_id=random_id)
            self._assert_live_epoch(epoch)
            if stored_id <= 1:
                raise RuntimeError("created topic conflicts with General")
            # Preserve Telegram's exact confirmed ID even if subsequent verification
            # or config persistence fails; never create a replacement on retry.
            with _db(root) as db:
                db.execute("UPDATE forum_topics SET topic_id=? WHERE topic_key=?", (stored_id, key))
        if stored_id <= 1:
            raise RuntimeError("saved topic conflicts with General")
        return await self._verify_topic(chat_id, stored_id, title=title)

    async def _create_topic(self, chat_id: int, title: str, *, random_id: int) -> int:
        from telethon import functions
        peer = await self.adapter._input_peer_for(chat_id)
        response = await self._audit_request(
            functions.messages.CreateForumTopicRequest(peer=peer, title=title, random_id=random_id)
        )
        return _confirmed_topic_id(response, random_id=random_id, peer=peer, title=title)

    async def _ensure_operation_topic(self, chat_id: int) -> int:
        from .forum_audit import _numeric
        from telethon import utils
        title = f"Operations · Profile {self.profile_id}"
        raw = self._raw_config()
        known = None
        raw_id = _setup_chat_id(raw.get("chat_id"))
        if raw_id in {chat_id, utils.resolve_id(chat_id)[0]}:
            value = raw.get("operations_topic_id")
            if value is not None:
                known = _numeric(value)
                if known is None or known <= 1:
                    raise RuntimeError("invalid saved Operations topic ID")
        return await self._ensure_topic(chat_id, "operations", title,
                                        random_id=_stable_id(self.home, chat_id, title), known_id=known)

    async def _verify_and_prepare(self, chat_id: int) -> tuple[int, int, int]:
        async with self._topic_lock:
            return await self._verify_and_prepare_locked(chat_id)

    async def _verify_and_prepare_locked(self, chat_id: int) -> tuple[int, int, int]:
        from telethon import utils
        epoch = self._worker_generation
        entity, _ = await self._verify_forum(chat_id)
        # Only a live, matching, private forum Channel may be normalized/stored.
        chat_id = utils.get_peer_id(entity)
        await self._verify_topic(chat_id, 1)  # General may be renamed/localized.
        self._assert_live_epoch(epoch)
        operations = await self._ensure_operation_topic(chat_id)
        return chat_id, 1, operations

    async def setup(self, chat_id: int) -> bool:
        """Live-verify and persist a new destination; no saved verification is trusted."""
        async with self._configuration_lock:
            return await self._setup_locked(chat_id)

    async def _setup_locked(self, chat_id: int) -> bool:
        epoch = self._worker_generation
        self._assert_live_epoch(epoch)
        chat_id, general, operations = await self._verify_and_prepare(chat_id)
        self._assert_live_epoch(epoch)
        config = {"enabled": True, "chat_id": chat_id, "general_topic_id": general,
                  "operations_topic_id": operations,
                  "verified_entity": True, "verified_forum": True, "verified_rights": True}
        await self._stop_worker()
        self._assert_live_epoch(epoch + 1)
        self._persist_config(config)
        self._new_service(config, runtime_verified=True)
        self._set_operations_capture(True)
        self._start_worker()
        return True

    def _assert_live_epoch(self, epoch: int) -> None:
        runner = getattr(self.adapter, "gateway_runner", None)
        homes = getattr(runner, "_served_profile_homes", None)
        if (self._revoked or epoch != self._worker_generation
                or (isinstance(homes, dict) and self.home not in self._trusted_homes().values())):
            raise RuntimeError("audit profile lifecycle changed during verification")

    def disable(self) -> None:
        self._persist_config({"enabled": False})
        self._set_operations_capture(False)
        self._new_service({"enabled": False})
        self._worker_generation += 1
        if self._task:
            self._task.cancel()
            self._task = None

    def _persist_config(self, audit: dict[str, Any]) -> None:
        from hermes_cli.config import atomic_config_write, read_user_config_raw
        path = self.home / "config.yaml"
        config = read_user_config_raw(path)
        if not isinstance(config, dict):
            raise ValueError("active profile config is not a mapping")
        platforms = config.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            raise ValueError("Telegram profile config has an invalid structure")
        telegram = platforms.setdefault("telegram", {})
        if not isinstance(telegram, dict):
            raise ValueError("Telegram profile config has an invalid structure")
        extra = telegram.setdefault("extra", {})
        if not isinstance(extra, dict):
            raise ValueError("Telegram extra config has an invalid structure")
        if audit.get("enabled") is True:
            extra["forum_audit"] = dict(audit)
        else:
            current = extra.get("forum_audit")
            if isinstance(current, dict):
                current["enabled"] = False
                extra["forum_audit"] = current
        atomic_config_write(path, config)
        persisted = dict(extra.get("forum_audit", audit))
        for peer in list(_CONTROLLERS):
            if peer is self or peer.home != self.home:
                continue
            if peer._service and peer._service.config == persisted:
                continue  # Re-verification of unchanged configuration is idempotent.
            peer._set_operations_capture(False)
            peer._new_service(persisted)  # Requires this transport's own live rights check.
            peer._worker_generation += 1
            if peer._task:
                peer._task.cancel()
                peer._task = None
            if peer is peer._manager:
                if not isinstance(peer.adapter.config.extra, dict):
                    peer.adapter.config.extra = {}
                peer.adapter.config.extra["forum_audit"] = dict(persisted)
        if self is not self._manager:
            return  # Never replace the receiving adapter's primary config.
        adapter_extra = getattr(self.adapter.config, "extra", None)
        if not isinstance(adapter_extra, dict):
            adapter_extra = {}
            self.adapter.config.extra = adapter_extra
        adapter_extra["forum_audit"] = dict(extra.get("forum_audit", audit))


    def _source_home(self, source: Any) -> Path | None:
        try:
            from .forum_audit import _numeric
            from gateway.session_identity import identity_of
            if (getattr(source, "profile_route_rejected", False)
                    or _numeric(getattr(source, "user_id", None)) is None):
                return None
            identity = identity_of(source)
            if identity is not None:
                home = Path(identity.runtime_home).expanduser().resolve(strict=False)
                trusted = self._trusted_homes()
                if identity.multiplexed:
                    if trusted.get(identity.runtime_profile) != home:
                        return None
                elif home != self._manager.home:
                    return None
                transport = identity.adapter()
                if transport is not self.adapter:
                    return None
                runner = getattr(self.adapter, "gateway_runner", None)
                owning = getattr(runner, "_owning_profile", None)
                if callable(owning):
                    _, owner = owning(self.adapter, getattr(source, "platform", None))
                    primary = getattr(runner, "_primary_profile_name", None) or "default"
                    if identity.transport_profile != (owner or primary):
                        return None
                return home
            runner = getattr(self.adapter, "gateway_runner", None)
            if (getattr(getattr(runner, "config", None), "multiplex_profiles", False)
                    or getattr(source, "profile", None)):
                return None  # Multiplex events require the pinned ingress identity.
            resolver = getattr(self.adapter, "_settings_home", None)
            value = resolver(source) if callable(resolver) else self._manager.home
            if value is None:
                return None
            home = Path(value).expanduser().resolve(strict=False)
            return home if home == self._manager.home else None
        except Exception:
            # Fail closed rather than silently routing an event into this profile.
            return None





    async def _send_text(self, chat_id: int, topic_id: int | None, text: str,
                         event_key: str = "") -> Any:
        from telethon import functions
        service = self._service
        peer = await self.adapter._input_peer_for(chat_id)
        from .topic_delivery import topic_reply_header
        result = None
        for index, chunk in enumerate(_text_chunks(str(text), 3000)):
            if service is not self._service or not service.enabled:
                raise RuntimeError("audit destination changed before send")
            request = functions.messages.SendMessageRequest(
                peer=peer, message=chunk,
                random_id=_stable_id(self.home, chat_id, topic_id, event_key or text, index),
                no_webpage=True, entities=None,
                reply_to=topic_reply_header(None, topic_id),
            )
            result = await self._audit_request(request)
        return result

    def _cooldown(self) -> float:
        manager = self._manager
        until = manager._flood_until
        for controller in manager._profiles.values():
            if (controller._service and controller._service.forum_id is not None
                    and controller._service._storage_root.exists()):
                until = max(until, controller._service.flood_until())
        manager._flood_until = until
        return until

    async def _audit_request(self, request: Any) -> Any:
        runner = getattr(self.adapter, "gateway_runner", None)
        homes = getattr(runner, "_served_profile_homes", None)
        if self._revoked or (isinstance(homes, dict) and
                             self.home not in self._trusted_homes().values()):
            raise RuntimeError("Telegram audit profile is no longer served")
        until = self._cooldown()
        if until > time.time():
            error = RuntimeError("Telegram audit transport cooldown")
            error.seconds = max(1, until - time.time())
            raise error
        try:
            return await self.adapter._mtproto_rich_request(request)
        except Exception as exc:
            seconds = getattr(exc, "seconds", None)
            if seconds is not None:
                until = time.time() + max(1, float(seconds))
                self._manager._flood_until = max(self._manager._flood_until, until)
                for controller in self._manager._profiles.values():
                    if controller._service and controller._service.enabled:
                        controller._service.defer_flood(until)
            raise

    async def _drain_once(self) -> int:
        if self._cooldown() > time.time():
            return 0
        return await self._service.drain(send_text=self._send_text, limit=25)

    async def _drain_loop(self, generation: int | None = None) -> None:
        generation = self._worker_generation if generation is None else generation
        while (generation == self._worker_generation and self._service and self._service.enabled
               and not self._revoked and self.adapter._client is not None):
            try:
                await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Telegram forum audit background drain failed")
            await asyncio.sleep(2.0)

    async def command(self, source: Any, arguments: str, *, reply_to: Any = None) -> bool:
        """Owner-private commands act on the pinned runtime profile, never ambient home."""
        home = self._source_home(source)
        controller = self._profile_controller(home) if home else None
        if controller is None:
            await self.adapter.send(source.chat_id,
                                    "Forum audit profile identity could not be resolved; no configuration changed.",
                                    reply_to=reply_to)
            return True
        try:
            with controller._policy_scope():
                owner = (str(getattr(source, "chat_type", "")) in {"dm", "private", "direct"}
                         and self.adapter._authorized(source)
                         and self.adapter._is_owner_user(source.user_id, source))
        except Exception:
            owner = False
        if not owner:
            await self.adapter.send(source.chat_id, "Forum audit commands are owner-only in a private chat.",
                                    reply_to=reply_to)
            return True
        return await controller._command_profile(source, arguments, reply_to=reply_to)

    async def _command_profile(self, source: Any, arguments: str, *, reply_to: Any = None) -> bool:
        value = str(arguments or "").strip()
        if not value or value.lower() == "status":
            from .forum_audit import status_for_profile
            status = status_for_profile(self.home)
            await self.adapter.send(
                source.chat_id,
                f"Forum audit (profile {self.profile_id}): {'enabled' if status.get('enabled') else 'disabled'}. "
                f"Pending: {status.get('pending', 0)}; per-user topics: {status.get('mapped_users', 0)}.",
                reply_to=reply_to,
            )
            return True
        if value.lower() == "off":
            async with self._configuration_lock:
                await self._stop_worker()
                self.disable()
            await self.adapter.send(source.chat_id, "Private forum audit disabled for this profile.", reply_to=reply_to)
            return True
        if value.lower().startswith("setup"):
            parts = value.split()
            if len(parts) != 2:
                await self.adapter.send(source.chat_id, "Use `/logs setup <channel ID>`, `/logs setup off`, or `/logs status`.", reply_to=reply_to)
                return True
            if parts[1].lower() == "off":
                async with self._configuration_lock:
                    await self._stop_worker()
                    self.disable()
                await self.adapter.send(source.chat_id, "Private forum audit disabled for this profile.", reply_to=reply_to)
                return True
            chat_id = _setup_chat_id(parts[1])
            if chat_id is None:
                await self.adapter.send(source.chat_id, "Setup requires a numeric private forum channel ID (raw positive ID or canonical `-100…` ID); public usernames are not accepted.", reply_to=reply_to)
                return True
            try:
                await self.setup(chat_id)
            except Exception as exc:
                logger.warning("Telegram forum audit setup failed (%s)", type(exc).__name__)
                from telethon.errors import BotMethodInvalidError
                if isinstance(exc, BotMethodInvalidError):
                    await self.adapter.send(source.chat_id,
                        "Forum audit setup failed: Telegram rejected an unsupported bot method (BotMethodInvalidError). "
                        "The new destination was not enabled; existing configuration is unchanged.", reply_to=reply_to)
                    return True
                await self.adapter.send(source.chat_id, "Forum audit setup could not verify the private forum, General topic, Operations topic, or bot permissions. The new destination was not enabled; any existing configuration is unchanged.", reply_to=reply_to)
                return True
            await self.adapter.send(source.chat_id, "Private forum audit verified and enabled for this profile.", reply_to=reply_to)
            return True
        await self.adapter.send(source.chat_id, "Use `/logs setup <channel ID>`, `/logs setup off`, or `/logs status`.", reply_to=reply_to)
        return True


def install(adapter: Any, profile_home: str | Path) -> ForumAuditTransport:
    """Install the adapter-local hooks; safe to call once per adapter factory."""
    current = getattr(adapter, "_forum_audit_transport", None)
    if current is not None:
        return current
    controller = ForumAuditTransport(adapter, profile_home)
    adapter._forum_audit_transport = controller
    return controller


__all__ = ["ForumAuditTransport", "install"]
