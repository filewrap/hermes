"""Rich streaming responses for Telegram DM turns (Beta).

Separate from GuestBot streaming: this is the per-turn rich draft stream for
ordinary (non-guest) turns in private 1-1 chats, behind the profile-wide
``platforms.telegram.streaming_response`` setting (default off). The guestbot
draft path is untouched — guest turns keep their own text-draft sink.

Mechanics (Telethon 1.45.0, layer 232 — constructor IDs and signatures
verified against the installed wheel, not from memory):

- Live draft via ``messages.setTyping`` with
  ``InputSendMessageRichMessageDraftAction`` (rich) or
  ``SendMessageTextDraftAction`` (fallback). One fixed ``random_id`` per
  generation: the same id animates the draft, a new id would replace it.
- Reasoning deltas arrive first and render as a real Thinking block
  (``PageBlockThinking``) in the draft; once content flows, the draft shows
  the accumulated Markdown (``InputRichMessageMarkdown``). The Thinking
  block is draft-only by design — it never reaches the final message.
- 1 s trailing-edge throttle: a frame goes out immediately when the gate is
  open, otherwise only the latest frame is kept pending and a background
  pump flushes it when the gate opens. Keepalive re-pushes the current
  frame every 20 s against the ~30 s draft TTL.
- Sync-draft before finalising: the final message is adopted by the client
  only when it shares >= 1 leading character with the last draft's text,
  so just before the final send the sink pushes one draft equal to the
  first chunk of the completed final (awaited, bounded), then stops its
  pump so no ghost draft can land after the final.
- Fail-open everywhere: a dead preview never breaks the turn. Rich
  rejection falls back to a text draft; an unresolvable peer, a
  private-only violation (``TEXTDRAFT_PEER_INVALID``) or three consecutive
  transient failures disable the sink for the rest of the turn; the final
  still goes out through the normal delivery path.
- No edit-loop streaming anywhere: groups get normal replies, no
  streaming. Drafts are private 1-1 only by Telegram's own rule.
- No Stop button: ``can_stop`` is deliberately left unset. A Stop press
  arrives as ``UpdateUserTyping`` with ``SendMessageStopDraftAction`` and
  Hermes has no handler for it yet, so a visible Stop button would be a
  dead promise. Stop handling is a separate build, not smuggled in here.

Peer resolution (the live DM ``PeerIdInvalidError`` gap): raw requests do
not resolve peers — ``SetTypingRequest`` needs a real ``InputPeer``, and
the adapter's ``_peer_for`` is cache-only (it returns the bare chat id
when uncached). The sink resolves through ``_input_peer_for`` (adapter
cache, then Telethon's entity cache, then its network fallback) and
validates the result is an ``InputPeer`` before use. On
``PeerIdInvalidError`` it drops the stale cached peer and re-resolves
fresh from the network (``users.GetUsersRequest`` with ``access_hash=0``,
which works for a bot when the user has messaged it privately), retries
once, and then disables itself.
"""
import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Trailing-edge throttle: at most one draft update per interval; the pump
# flushes the latest pending frame when the gate opens.
_DRAFT_MIN_INTERVAL_S = 1.0
# A live draft expires after ~30 s; re-push the current frame well inside it.
_DRAFT_KEEPALIVE_S = 20.0
# Draft payload cap, near Telegram's 4096-char text-draft limit.
_DRAFT_MAX_CHARS = 4000
# Thinking-block cap: reasoning is a preview, not the answer.
_DRAFT_THINKING_MAX_CHARS = 2000
_DRAFT_SEND_TIMEOUT_S = 10.0
_SYNC_DRAFT_TIMEOUT_S = 10.0
# Safety net: the pump never runs past this, even if a turn forgets to stop it.
_PUMP_MAX_LIFETIME_S = 300.0
# Consecutive transient failures before the sink gives up for the turn.
_MAX_CONSECUTIVE_FAILURES = 3
# crc32(b'InputPeer'): a raw request needs a real input peer, never a bare Peer.
_INPUT_PEER_SUBCLASS_ID = 0xC91C90B6


def streaming_response_enabled(adapter: Any) -> bool:
    """Beta rich streaming drafts for DM turns. Off unless the profile opts in."""
    runtime = getattr(adapter, "_streaming_response_runtime", None)
    if runtime is not None:
        return runtime
    extra = getattr(adapter.config, "extra", {}) or {}
    if "streaming_response" in extra:
        return adapter._truthy_setting(extra.get("streaming_response"), default=False)
    return False


def persist_streaming_response_setting(adapter: Any, source: Any, enabled: bool) -> None:
    """Persist the profile-wide streaming-response toggle; never changes BotFather settings."""
    from .guestbot import _persist_guestbot_flag

    # The persister is flag-generic (key + runtime attr); it writes
    # platforms.telegram.streaming_response in config.yaml and updates the
    # live adapter's extra + runtime override in the same call.
    _persist_guestbot_flag(adapter, source, key="streaming_response",
                           runtime_attr="_streaming_response_runtime", enabled=enabled)


def _truncate_frame(markdown: str, limit: int) -> str:
    """Prefix-cut a draft payload; close an open code fence so the preview parses."""
    text = (markdown or "")[:limit]
    if text.count("```") % 2 == 1:
        text = text.rstrip() + "\n```"
    return text


def _is_input_peer(peer: Any) -> bool:
    return getattr(peer, "SUBCLASS_OF_ID", None) == _INPUT_PEER_SUBCLASS_ID


class ResponseStreamDraft:
    """Ephemeral rich streaming preview for one non-guest DM turn.

    Reasoning deltas arrive first and content deltas follow; the draft is
    the cumulative preview (Thinking block while reasoning-only, Markdown
    once content flows), sent as a rich draft action via
    ``SetTypingRequest`` to the DM. The draft is only a preview — it
    expires on its own and the complete final message still goes out
    through the normal delivery path, so this sink never declares or
    delivers a final.

    Draft frames are prefix-stable: reasoning freezes once the first
    content delta arrives (later reasoning would have nowhere to go
    without rewriting earlier text), content only appends, and the length
    cap is a fixed prefix cut. Updates are trailing-edge throttled so a
    token stream does not send one ``setTyping`` per token, and a pump
    task re-pushes the current frame inside the draft TTL. Every failure
    is swallowed at debug: a dead preview must never break the turn or
    the final send.
    """

    def __init__(self, adapter: Any, chat_id: Any, *, loop: Any = None,
                 min_interval_s: float = _DRAFT_MIN_INTERVAL_S,
                 keepalive_s: float = _DRAFT_KEEPALIVE_S,
                 max_chars: int = _DRAFT_MAX_CHARS,
                 time_fn=time.monotonic) -> None:
        self._adapter = adapter
        self._chat_id = str(chat_id)
        self._loop = loop
        self._min_interval_s = min_interval_s
        self._keepalive_s = keepalive_s
        self._max_chars = max_chars
        self._time_fn = time_fn
        # One fixed id per generation: the same id animates the draft.
        # Never reuse it for the final send — adoption is purely heuristic.
        self._random_id = secrets.randbits(63) or 1
        self._reasoning_parts: list[str] = []
        self._content_parts: list[str] = []
        self._last_sent_key: Optional[tuple[str, str]] = None
        self._last_sent_at = 0.0
        self._next_allowed_at = 0.0
        self._pending = False
        self._rich_supported = True
        self._disabled = False
        self._finished = False
        self._consecutive_failures = 0
        self._peer: Any = None
        self._pump_future: Any = None
        self._pump_started_at: Optional[float] = None
        self._first_draft_logged = False

    def _frame(self) -> Optional[tuple[str, str]]:
        """(kind, payload) for the current draft, or None when nothing to show."""
        reasoning = "".join(self._reasoning_parts)
        content = "".join(self._content_parts)
        if content:
            text = _truncate_frame(content, self._max_chars)
            if not text:
                return None
            return ("rich", text) if self._rich_supported else ("text", text)
        if reasoning:
            return ("thinking", reasoning[:_DRAFT_THINKING_MAX_CHARS])
        return None

    def on_reasoning(self, text: str) -> None:
        if not text or self._content_parts:
            return
        self._reasoning_parts.append(text)
        self._maybe_send()

    def on_content(self, text: str) -> None:
        if not text:
            return
        self._content_parts.append(text)
        self._maybe_send()

    def on_delta(self, text: Optional[str]) -> None:
        """Delta-sink interface the turn's ``stream_delta_callback`` closure calls."""
        if text:
            self.on_content(text)

    def _maybe_send(self) -> None:
        if self._disabled or self._finished:
            return
        frame = self._frame()
        if frame is None or frame == self._last_sent_key:
            return
        now = self._time_fn()
        if now < self._next_allowed_at or now - self._last_sent_at < self._min_interval_s:
            # Trailing edge: keep only the latest frame; the pump flushes it
            # when the gate opens.
            self._pending = True
            self._ensure_pump()
            return
        self._pending = False
        self._send_now(frame)

    def _send_now(self, frame: tuple[str, str]) -> None:
        """Dispatch one draft frame; the pump covers keepalive and retries."""
        self._last_sent_at = self._time_fn()
        self._ensure_pump()
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Agent callbacks run on the worker thread, which has no loop;
                # the runner always supplies one, so this is a dropped preview.
                logger.debug("Streaming response draft dropped: no event loop on this thread")
                return
        try:
            asyncio.run_coroutine_threadsafe(self._send_frame(*frame), loop)
        except RuntimeError:
            logger.debug("Streaming response draft dropped: event loop is closed", exc_info=True)

    def _ensure_pump(self) -> None:
        """Start the keepalive/flush pump once; it exits on finish, cancel, or age."""
        if self._pump_future is not None or self._finished:
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        try:
            self._pump_future = asyncio.run_coroutine_threadsafe(self._pump(), loop)
        except RuntimeError:
            logger.debug("Streaming response pump dropped: event loop is closed", exc_info=True)

    async def _pump(self) -> None:
        """Re-push the current frame inside the draft TTL; flush pending frames at the gate."""
        self._pump_started_at = self._time_fn()
        try:
            while not self._finished and not self._disabled:
                await asyncio.sleep(0.5)
                if self._finished or self._disabled:
                    return
                if self._time_fn() - (self._pump_started_at or 0.0) > _PUMP_MAX_LIFETIME_S:
                    return
                frame = self._frame()
                if frame is None:
                    continue
                now = self._time_fn()
                if self._pending and now >= self._next_allowed_at and now - self._last_sent_at >= self._min_interval_s:
                    self._pending = False
                    self._last_sent_at = now
                    await self._send_frame(*frame)
                elif (self._last_sent_key is not None
                        and now - self._last_sent_at >= self._keepalive_s):
                    self._last_sent_at = now
                    await self._send_frame(*frame)
        except asyncio.CancelledError:
            return

    def finish_sync_draft(self, final_text: str) -> None:
        """Push one draft equal to the first chunk of the completed final, awaited.

        Called on the worker thread just before the final send. The client
        adopts the final only when it shares >= 1 leading character with
        the last draft's text; when the last draft already starts the final
        there is nothing to sync. Always stops the pump afterwards, so no
        ghost draft can land after the final message.
        """
        if self._finished:
            return
        self._finished = True
        self._cancel_pump()
        if self._disabled or not final_text or not str(final_text).strip():
            return
        if self._last_sent_key is None:
            return  # never showed a draft; nothing to sync
        first = _truncate_frame(str(final_text), self._max_chars)
        if not first:
            return
        last_text = self._last_sent_key[1]
        if last_text and first and last_text[0] == first[0]:
            return
        kind = "rich" if self._rich_supported else "text"
        self._send_sync(kind, first)

    def _send_sync(self, kind: str, text: str) -> None:
        """Schedule one draft frame and block (bounded) until it lands or fails."""
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("Streaming response sync draft dropped: no event loop on this thread")
                return
        try:
            future = asyncio.run_coroutine_threadsafe(self._send_frame(kind, text), loop)
        except RuntimeError:
            logger.debug("Streaming response sync draft dropped: event loop is closed", exc_info=True)
            return
        try:
            future.result(timeout=_SYNC_DRAFT_TIMEOUT_S)
        except Exception:
            logger.debug("Streaming response sync draft did not complete in time", exc_info=True)

    def cancel(self) -> None:
        """Stop the pump; the live draft (if any) simply expires on its own."""
        self._finished = True
        self._cancel_pump()

    def _cancel_pump(self) -> None:
        future = self._pump_future
        self._pump_future = None
        if future is not None:
            with contextlib.suppress(Exception):
                future.cancel()

    async def _resolve_input_peer(self) -> Any:
        """Return a validated InputPeer for this DM, or None when unresolvable.

        Fast path: the adapter's resolver (its peer cache, Telethon's
        entity cache, Telethon's network fallback). Slow path: drop any
        stale cached peer and resolve fresh from the network — a bot may
        resolve a user who messaged it privately with access_hash=0.
        """
        if self._peer is not None:
            return self._peer
        adapter = self._adapter
        peer = None
        try:
            peer = await adapter._input_peer_for(self._chat_id)
        except Exception:
            logger.debug("Streaming response: adapter peer resolution failed for chat %s",
                         self._chat_id, exc_info=True)
        if _is_input_peer(peer):
            self._peer = peer
            return peer
        try:
            cache = getattr(adapter, "_peer_cache", None)
            if isinstance(cache, dict):
                cache.pop(str(self._chat_id), None)
        except Exception:
            pass
        client = getattr(adapter, "_client", None)
        if client is None:
            return None
        try:
            user_id = int(str(self._chat_id).strip())
        except (TypeError, ValueError):
            return None
        try:
            from telethon import functions, types, utils as tl_utils

            users = await client(functions.users.GetUsersRequest(
                [types.InputUser(user_id, 0)]))
            user = users[0] if users else None
            if user is None or isinstance(user, types.UserEmpty):
                return None
            peer = tl_utils.get_input_peer(user)
        except Exception:
            logger.debug("Streaming response: fresh peer resolution failed for chat %s",
                         self._chat_id, exc_info=True)
            return None
        if not _is_input_peer(peer):
            return None
        self._peer = peer
        remember = getattr(adapter, "_remember_peer", None)
        if callable(remember):
            try:
                remember(self._chat_id, peer)
            except Exception:
                logger.debug("Streaming response: could not cache resolved peer", exc_info=True)
        return peer

    def _build_action(self, kind: str, text: str) -> Any:
        """Draft action for one frame. ``can_stop`` is deliberately unset:
        a Stop press has no handler yet, so no Stop button is shown."""
        from telethon import types

        if kind == "thinking":
            rich = types.InputRichMessage(blocks=[
                types.PageBlockThinking(text=types.TextPlain(text=text)),
            ])
            return types.InputSendMessageRichMessageDraftAction(
                rich_message=rich, random_id=self._random_id)
        if kind == "rich":
            from .mtproto_rich import input_rich_markdown

            return types.InputSendMessageRichMessageDraftAction(
                rich_message=input_rich_markdown(text), random_id=self._random_id)
        return types.SendMessageTextDraftAction(
            text=types.TextWithEntities(text=text, entities=[]),
            random_id=self._random_id)

    async def _send_frame(self, kind: str, text: str, *, _retry_peer: bool = True) -> bool:
        """Send one draft frame. False on any failure; the turn never depends on it."""
        if self._disabled:
            return False
        peer = await self._resolve_input_peer()
        if peer is None:
            self._disabled = True
            logger.debug("Streaming response: peer unresolvable for chat %s; drafts disabled",
                         self._chat_id)
            return False
        try:
            action = self._build_action(kind, text)
        except Exception:
            logger.debug("Streaming response: could not build draft action", exc_info=True)
            return False
        client = getattr(self._adapter, "_client", None)
        if client is None:
            return False
        try:
            from telethon import errors, functions

            await asyncio.wait_for(
                client(functions.messages.SetTypingRequest(peer=peer, action=action)),
                timeout=_DRAFT_SEND_TIMEOUT_S,
            )
        except errors.FloodWaitError as err:
            # Telethon already slept out waits under its threshold; a raised
            # wait is long, so park the sink behind it and keep the frame pending.
            self._next_allowed_at = self._time_fn() + max(
                1.0, float(getattr(err, "seconds", 1) or 1))
            self._pending = True
            logger.debug("Streaming response: draft flood-waited for chat %s", self._chat_id)
            return self._note_failure()
        except errors.PeerIdInvalidError:
            if _retry_peer:
                # Stale cached access hash: drop it and resolve fresh once.
                self._peer = None
                try:
                    cache = getattr(self._adapter, "_peer_cache", None)
                    if isinstance(cache, dict):
                        cache.pop(str(self._chat_id), None)
                except Exception:
                    pass
                return await self._send_frame(kind, text, _retry_peer=False)
            self._disabled = True
            logger.warning("Streaming response: peer invalid for chat %s; drafts disabled for this turn",
                           self._chat_id)
            return self._note_failure()
        except errors.BadRequestError as err:
            message = str(getattr(err, "message", "") or err)
            if "TEXTDRAFT_PEER_INVALID" in message:
                self._disabled = True
                logger.debug("Streaming response: drafts unsupported for chat %s", self._chat_id)
                return False
            if kind in ("rich", "thinking"):
                # Rich rejected (empty/unsupported markup): fall back to a
                # text draft for this and every later frame.
                self._rich_supported = False
                self._pending = True
                logger.debug("Streaming response: rich draft rejected for chat %s (%s); falling back to text",
                             self._chat_id, message)
                return await self._send_frame("text", text, _retry_peer=False)
            logger.debug("Streaming response: draft rejected for chat %s: %s",
                         self._chat_id, message)
            return self._note_failure()
        except (TimeoutError, OSError) as err:
            # Transient transport failure: keep the frame pending for the pump.
            self._next_allowed_at = self._time_fn() + 5.0
            self._pending = True
            logger.debug("Streaming response: draft send timed out for chat %s: %s",
                         self._chat_id, err)
            return self._note_failure()
        except Exception:
            logger.debug("Streaming response: draft send failed for chat %s",
                         self._chat_id, exc_info=True)
            return self._note_failure()
        self._consecutive_failures = 0
        self._last_sent_key = (kind, text)
        self._last_sent_at = self._time_fn()
        if not self._first_draft_logged:
            self._first_draft_logged = True
            # INFO, not debug: the first draft proves the sink is live. Length only;
            # the draft text may contain the user's prompt or the model's reply.
            logger.info("Streaming response draft sent: chat=%s draft_chars=%d",
                        self._chat_id, len(text))
        return True

    def _note_failure(self) -> bool:
        """Count a transient failure; give up for the turn after the bound."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._disabled = True
            logger.debug("Streaming response: %d consecutive draft failures for chat %s; drafts disabled",
                         self._consecutive_failures, self._chat_id)
        return False
