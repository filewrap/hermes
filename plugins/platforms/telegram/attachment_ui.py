"""Metadata-first Telegram attachment intake and explicit, scoped downloads.

The adapter owns authorization and event routing. Call :meth:`prepare_inbound`
only after the normal authorization gate, and call :meth:`handle_callback` only
after callback authorization has also succeeded. This module never reads an
attachment into Python memory: approved downloads are streamed to the existing
private document cache and checked against Hermes' configured inbound cap.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from gateway.telegram_personal_settings import (
    DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES,
    DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES,
    ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES,
    load_personal_settings,
)

logger = logging.getLogger(__name__)
MAX_ALBUM_ATTACHMENTS = 10
_REQUEST_TTL_SECONDS = 30 * 60
_FILENAME_RE = re.compile(r"[^\w. -]+", re.UNICODE)


@dataclass(frozen=True)
class AttachmentMetadata:
    """Small, safe-to-forward facts about one Telegram attachment."""

    index: int
    filename: str
    mime_type: str
    kind: str
    size_bytes: int | None
    caption: str
    message_id: str
    download_mode: str
    download_limit_bytes: int
    deferred: bool
    callback_data: str | None = None
    auto_download_limit_bytes: int = 0

    def context_line(self) -> str:
        size = format_size(self.size_bytes) if self.size_bytes is not None else "unknown size"
        caption = f"; caption: {self.caption}" if self.caption else ""
        state = "download deferred; explicit download available" if self.deferred else "ready for automatic download"
        limits = (f"; explicit download limit {format_size(self.download_limit_bytes)}; "
                  f"effective automatic limit {format_size(self.auto_download_limit_bytes)}")
        return (f"[Telegram attachment {self.index}: {self.filename or 'unnamed file'}; "
                f"MIME {self.mime_type}; type {self.kind}; size {size}{caption}; {state}{limits}]")


@dataclass(frozen=True)
class AttachmentIntake:
    """Metadata delivered immediately to the agent, plus the deferred-download controls."""

    attachments: tuple[AttachmentMetadata, ...]
    note: str
    acknowledgement_emoji: str

    @property
    def automatic_messages(self) -> tuple[Any, ...]:
        """Messages the adapter may pass to its auto-cache path after this policy check."""
        return tuple(item._message for item in self.attachments if not item.deferred)  # type: ignore[attr-defined]


def format_size(size: int | None) -> str:
    if size is None or int(size) < 0:
        return "unknown size"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _safe_filename(value: Any) -> str:
    raw = Path(str(value or "")).name.replace("\x00", "").strip()
    clean = _FILENAME_RE.sub("_", raw)
    return clean[:160] or "attachment"


def _mime_and_kind(message: Any) -> tuple[str, str, str]:
    info = getattr(message, "file", None)
    filename = _safe_filename(getattr(info, "name", "") or "")
    mime = str(getattr(info, "mime_type", "") or "").strip().lower()
    guessed, _encoding = mimetypes.guess_type(filename)
    mime = mime or guessed or "application/octet-stream"
    if getattr(message, "photo", None) and mime == "application/octet-stream":
        mime = "image/jpeg"
    major = mime.split("/", 1)[0] if "/" in mime else "document"
    kind = major if major in {"image", "video", "audio"} else "document"
    return filename, mime, kind


def _attachment_messages(messages: Any) -> list[Any]:
    if isinstance(messages, (list, tuple)):
        return list(messages[:MAX_ALBUM_ATTACHMENTS])
    return [messages] if messages is not None else []


def effective_download_cap() -> int:
    """Read the gateway cap used by the existing in-memory automatic media cache."""
    from gateway.platforms.base import get_inbound_media_max_bytes
    return int(get_inbound_media_max_bytes())


def automatic_download_cap() -> int:
    """The ordinary inbound cache cap; this path still uses the existing cache implementation."""
    return effective_download_cap()


def explicit_operator_download_cap() -> int | None:
    """Return a positive operator-configured cap, but not the inherited 128 MiB default.

    The merged configuration always contains defaults, so inspect the user's raw YAML for the
    same-as-default case. If that is unavailable, a non-default effective value is still treated
    as an operator override. No config is modified here.
    """
    cap = effective_download_cap()
    explicitly_set = False
    try:
        from hermes_cli.config import read_user_config_raw

        raw = read_user_config_raw()
        section = raw.get("gateway") if isinstance(raw, dict) else None
        explicitly_set = isinstance(section, dict) and "max_inbound_media_bytes" in section
    except Exception:
        pass
    from gateway.platforms.base import DEFAULT_INBOUND_MEDIA_MAX_BYTES
    if not explicitly_set and cap == DEFAULT_INBOUND_MEDIA_MAX_BYTES:
        return None
    return cap if cap > 0 else None


def effective_explicit_download_limit(settings: dict[str, Any] | None = None) -> int:
    """Resolve the user's streamed-download ceiling against any explicit operator ceiling."""
    settings = settings or {}
    try:
        selected = int(settings.get(
            "attachment_explicit_download_limit_bytes",
            DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES,
        ))
    except (TypeError, ValueError):
        selected = DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES
    if selected not in ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES:
        selected = DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES
    operator_cap = explicit_operator_download_cap()
    return min(selected, operator_cap) if operator_cap else selected


def _scope_home(adapter: Any, source: Any) -> Path:
    runner = getattr(adapter, "gateway_runner", None)
    resolver = getattr(runner, "_resolve_profile_home_for_source", None)
    home = resolver(source) if callable(resolver) else None
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home).expanduser().resolve()


def _threshold(settings: dict[str, Any]) -> int:
    try:
        return int(settings.get("attachment_auto_download_threshold_bytes", DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES


def _is_attachment(message: Any) -> bool:
    return bool(getattr(message, "photo", None) or getattr(message, "document", None))


class TelegramAttachmentUI:
    """Per-adapter controller for authorized Telegram attachment flows."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._pending: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for token, request in list(self._pending.items()):
            if now - request["created"] > _REQUEST_TTL_SECONDS:
                self._pending.pop(token, None)

    async def _ack(self, message: Any, source: Any, emoji: str) -> None:
        message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
        react = getattr(self.adapter, "send_reaction", None)
        if message_id is None or not callable(react):
            return
        try:
            await react(source.chat_id, message_id, emoji)
        except Exception:
            logger.debug("Telegram attachment acknowledgement failed", exc_info=True)

    def _new_download_request(
        self, source: Any, message: Any, home: Path, session_key: str,
        max_bytes: int | None = None,
    ) -> str:
        self._prune()
        token = uuid.uuid4().hex[:12]
        while token in self._pending:
            token = uuid.uuid4().hex[:12]
        if max_bytes is None:
            user_id = str(getattr(source, "user_id", "") or "")
            settings = load_personal_settings(home, user_id) if user_id.isdigit() else {}
            max_bytes = effective_explicit_download_limit(settings)
        self._pending[token] = {
            "created": time.monotonic(), "home": str(home),
            "user_id": str(getattr(source, "user_id", "") or ""),
            "chat_id": str(getattr(source, "chat_id", "") or ""),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "session_key": str(session_key or ""), "message": message,
            "max_bytes": int(max_bytes),
        }
        return f"tdl:{token}"

    async def prepare_inbound(
        self,
        source: Any,
        messages: Any,
        *,
        authorized: bool,
        session_key: str = "",
    ) -> AttachmentIntake:
        """Acknowledge an authorized attachment and return metadata without downloading it.

        Authorization is deliberately a required, explicit argument: callers must run
        the gateway/tenant allowlist gate first. Deferred files get a one-shot callback
        bound to profile, user, chat, topic, and session; ``note`` is suitable for the
        inbound agent event even when no bytes have been fetched.
        """
        if not authorized:
            raise PermissionError("Telegram attachment intake requires an authorized sender")
        original_messages = list(messages) if isinstance(messages, (list, tuple)) else ([messages] if messages is not None else [])
        overflow = len(original_messages) > MAX_ALBUM_ATTACHMENTS
        batch = [message for message in _attachment_messages(original_messages) if _is_attachment(message)]
        if not batch:
            return AttachmentIntake((), "", "👀")
        home = _scope_home(self.adapter, source)
        settings = load_personal_settings(home, str(source.user_id)) if str(getattr(source, "user_id", "") or "").isdigit() else {}
        mode = str(settings.get("attachment_download_mode", "auto"))
        if mode not in {"auto", "explicit"}:
            mode = "auto"
        threshold = _threshold(settings)
        auto_cap = automatic_download_cap()
        auto_limit = min(threshold, auto_cap) if auto_cap else threshold
        explicit_limit = effective_explicit_download_limit(settings)
        emoji = "🆒" if len(batch) > 1 else "👀"
        if settings.get("reactions_enabled", True):
            await asyncio.gather(*(self._ack(message, source, emoji) for message in batch))
        results: list[AttachmentMetadata] = []
        for index, message in enumerate(batch, 1):
            info = getattr(message, "file", None)
            raw_size = getattr(info, "size", None)
            try:
                size = int(raw_size) if raw_size is not None and int(raw_size) >= 0 else None
            except (TypeError, ValueError):
                size = None
            filename, mime, kind = _mime_and_kind(message)
            # Unknown sizes are metadata-only; auto caching stays under the existing
            # safety cap even when a user's displayed threshold is higher.
            within_policy = size is not None and mode == "auto" and size <= auto_limit
            deferred = not within_policy
            callback = (self._new_download_request(
                source, message, home, session_key, max_bytes=explicit_limit,
            ) if deferred else None)
            metadata = AttachmentMetadata(
                index=index, filename=filename, mime_type=mime, kind=kind,
                size_bytes=size, caption=str(getattr(message, "message", "") or "").strip()[:1000],
                message_id=str(getattr(message, "id", "") or ""), download_mode=mode,
                download_limit_bytes=explicit_limit, deferred=deferred, callback_data=callback,
                auto_download_limit_bytes=auto_limit,
            )
            # Keep the originating message only in the short-lived in-process intake
            # object so the adapter can pass safe automatic items into its own cache.
            object.__setattr__(metadata, "_message", message)
            results.append(metadata)
        note_lines = [item.context_line() for item in results]
        if overflow:
            note_lines.append(f"[Only the first {MAX_ALBUM_ATTACHMENTS} album attachments were inspected.] ")
        note_lines.append(
            f"Automatic downloads use the {format_size(auto_limit)} effective limit "
            f"(personal threshold {format_size(threshold)}; inbound cache safety cap "
            f"{format_size(auto_cap) if auto_cap else 'disabled'}). Explicit authorized downloads "
            f"may stream up to {format_size(explicit_limit)}; Telegram/provider limits may also apply."
        )
        if any(item.deferred for item in results):
            note_lines.append("Deferred files require an explicit authorized download callback.")
        return AttachmentIntake(tuple(results), "\n".join(note_lines), emoji)

    @staticmethod
    def download_prompt(intake: AttachmentIntake) -> tuple[str, Any] | None:
        """Return a caption and native callback keyboard for deferred items, if any."""
        pending = [item for item in intake.attachments if item.deferred and item.callback_data]
        if not pending:
            return None
        from .mtproto_rich import inline_markup
        cap = max((item.download_limit_bytes for item in pending), default=0)
        auto_limit = max((item.auto_download_limit_bytes for item in pending), default=0)
        cap_text = format_size(cap) if cap else "no limit configured"
        buttons = []
        for item in pending:
            name = item.filename[:28]
            size = format_size(item.size_bytes) if item.size_bytes is not None else "unknown size"
            buttons.append([(f"Download {name} · {size}", item.callback_data, "primary")])
        text = ("Attachment received. Choose a file to download explicitly. "
                f"Explicit receive limit: {cap_text}. Automatic-download safety limit: "
                f"{format_size(auto_limit)}. These are receive limits only; Telegram/provider limits may also apply.")
        return text, inline_markup(buttons)

    async def handle_callback(
        self,
        event: Any,
        source: Any,
        data: str,
        *,
        authorized: bool,
        session_key: str = "",
    ) -> dict[str, Any] | None:
        """Consume one authorized download callback and stream it into the media cache.

        Returns a dict containing a ``CachedMedia``-compatible object and event
        note. ``None`` means this is not an attachment callback. Unauthorized calls
        are consumed silently: there is no callback answer, disk access, or download.
        """
        if not str(data).startswith("tdl:"):
            return None
        if not authorized:
            return {"handled": True, "authorized": False}
        token = str(data).partition(":")[2]
        self._prune()
        request = self._pending.get(token)
        if not request:
            await self._answer(event, "This download request expired.")
            return {"handled": True, "expired": True}
        home = _scope_home(self.adapter, source)
        valid = (
            request["home"] == str(home)
            and request["user_id"] == str(getattr(source, "user_id", "") or "")
            and request["chat_id"] == str(getattr(source, "chat_id", "") or "")
            and request["thread_id"] == str(getattr(source, "thread_id", "") or "")
            and request["session_key"] == str(session_key or "")
        )
        if not valid:
            await self._answer(event, "This download belongs to a different Telegram session.")
            return {"handled": True, "expired": True}
        # One-shot before any await, to prevent duplicate callbacks from starting
        # concurrent downloads of the same attachment.
        self._pending.pop(token, None)
        await self._answer(event, "Downloading attachment…")
        max_bytes = int(request.get("max_bytes") or effective_explicit_download_limit(
            load_personal_settings(home, request["user_id"]),
        ))
        try:
            media = await self._stream_to_cache(request["message"], max_bytes=max_bytes)
        except Exception as exc:
            logger.info("Telegram explicit attachment download failed (%s)", type(exc).__name__)
            return {"handled": True, "error": self._limit_error(exc, max_bytes)}
        return {"handled": True, "media": media, "note": media.context_note()}

    async def _answer(self, event: Any, text: str) -> None:
        answer = getattr(event, "answer", None)
        if callable(answer):
            try:
                await answer(text)
            except Exception:
                logger.debug("Could not answer Telegram attachment callback", exc_info=True)

    @staticmethod
    def _limit_error(exc: Exception, max_bytes: int | None = None) -> str:
        if isinstance(exc, OSError) and "space" in str(exc).lower():
            return "Attachment was not downloaded because the available disk budget is insufficient."
        cap = max_bytes if max_bytes is not None else DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES
        return (f"Attachment was not downloaded. The effective explicit receive limit is "
                f"{format_size(cap)}; Telegram/provider limits may also apply.")

    async def _stream_to_cache(self, message: Any, *, max_bytes: int | None = None) -> Any:
        client = getattr(self.adapter, "_client", None)
        if client is None:
            raise RuntimeError("Telegram client is not connected")
        info = getattr(message, "file", None)
        declared = getattr(info, "size", None)
        from gateway.platforms.base import (
            CachedMedia, get_document_cache_dir, validate_inbound_media_size,
        )
        limit = int(max_bytes) if max_bytes is not None else effective_explicit_download_limit()
        try:
            declared_size = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size < 0:
            declared_size = None
        if declared_size is not None and limit:
            validate_inbound_media_size(declared_size, media_type="Telegram attachment", max_bytes=limit)
        filename, mime, kind = _mime_and_kind(message)
        cache_dir = Path(get_document_cache_dir()).expanduser().resolve()
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        disk_budget = int(shutil.disk_usage(cache_dir).free)
        if declared_size is not None and declared_size > disk_budget:
            raise OSError("insufficient available disk space for Telegram attachment")
        safe_name = _safe_filename(filename)
        final_path = cache_dir / f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
        if not final_path.resolve().is_relative_to(cache_dir):
            raise ValueError("Telegram attachment cache path escaped its secure cache")
        # Telethon writes chunk-by-chunk to this guarded file; no Python bytes buffer
        # is created, and a chunk that would cross either ceiling is rejected before write.
        try:
            fd = os.open(final_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as output:
                class BoundedWriter:
                    def __init__(self, wrapped):
                        self.wrapped = wrapped
                        self.written = 0

                    def write(self, chunk):
                        next_size = self.written + len(chunk)
                        validate_inbound_media_size(
                            next_size, media_type="Telegram attachment", max_bytes=limit,
                        )
                        if next_size > disk_budget:
                            raise OSError("insufficient available disk space for Telegram attachment")
                        count = self.wrapped.write(chunk)
                        self.written += count
                        return count

                    def __getattr__(self, name):
                        return getattr(self.wrapped, name)

                bounded = BoundedWriter(output)

                def progress(current: int, _total: int) -> None:
                    validate_inbound_media_size(int(current), media_type="Telegram attachment", max_bytes=limit)
                    if int(current) > disk_budget:
                        raise OSError("insufficient available disk space for Telegram attachment")

                await client.download_media(message, file=bounded, progress_callback=progress)
            actual = final_path.stat().st_size
            validate_inbound_media_size(actual, media_type="Telegram attachment", max_bytes=limit)
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
        from tools.credential_files import to_agent_visible_cache_path
        visible_path = to_agent_visible_cache_path(str(final_path))
        return CachedMedia(
            path=visible_path, media_type=mime, kind=kind,
            display_name=filename or safe_name,
        )
