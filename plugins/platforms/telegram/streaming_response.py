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
  (``PageBlockThinking``, its text italic) in the draft; once content flows, the frozen
  reasoning stays visible above the content (Thinking block + content
  blocks in one rich draft), so the reasoning is never lost mid-stream.
  The italic is draft-only: the final message's Reasoning section stays
  a code block (``_hmwa_prepend_reasoning``). A reasoning draft leads
  with a "(Thinking Ns)" prefix — N is whole seconds since the first
  delta arrived, computed at frame-build time so it updates whenever a
  frame goes out, with no standalone ticker — above a sliding window
  of the last 7 reasoning lines (the 8th line clears the 1st, the 9th
  clears the 2nd); there is no hidden-line count. The prefix stays
  until the agent responds with actual content, so a content frame
  carries no prefix and the frozen reasoning above it is the same
  7-line window.
- The typing-to-first-reasoning gap is covered by one static plain-text
  draft: the sink primes a ``"Thinking"`` draft (``SendMessageTextDraftAction``,
  same ephemeral draft mechanism, never a sent message) at turn setup, and
  it stays exactly that text while the turn is still in the LOAD lane
  (no delta yet) — the draft is never re-sent with different text. The
  ``•••`` typing indicator on the live draft carries all the motion; the
  text cycling that used to run here made the prime animation look worse
  and is gone. The first reasoning or content delta replaces it, going
  out immediately — a lane exit never waits behind the throttle gate.
  It only ever exists on a streaming turn — no sink, no placeholder.
  The Thinking block is draft-only by design — it never reaches the
  final message.
- 1 s trailing-edge throttle: a frame goes out immediately when the gate is
  open, otherwise only the latest frame is kept pending and a background
  pump flushes it when the gate opens. Keepalive re-pushes the current
  frame every 20 s against the ~30 s draft TTL.
- Sync-draft before finalising: the final message is adopted by the client
  only when it shares a leading prefix with the last draft's text,
  so just before the final send the sink pushes one draft equal to the
  first chunk of the completed final (awaited, bounded), then stops its
  pump so no ghost draft can land after the final. The "(Thinking Ns)"
  prefix is decoration, not draft content: the adoption check strips it
  before comparing, and a last frame that led with a Thinking block
  always needs the plain sync draft anyway.
- Fail-open everywhere: a dead preview never breaks the turn. Rich
  rejection falls back to a text draft; an unresolvable peer, a
  private-only violation (``TEXTDRAFT_PEER_INVALID``) or three consecutive
  transient failures disable the sink for the rest of the turn; the final
  still goes out through the normal delivery path.
- No edit-loop streaming anywhere: groups get normal replies, no
  streaming. Drafts are private 1-1 only by Telegram's own rule.
- Stop: every draft action carries ``can_stop=True`` (and
  ``keep_on_stop=True``, so the partial draft stays visible until the
  real message replaces it). A Stop press arrives as ``UpdateUserTyping``
  with ``SendMessageStopDraftAction``; the adapter routes it to the live
  sink by ``random_id`` (``request_stop``), which halts the draft pump
  and fires ``on_stop`` so the runner interrupts the generation. The
  streamed partial is then delivered as the final message, so the
  stopped text is preserved instead of vanishing with the draft.
- Control framework: the sink tracks one lane (``load`` → ``reasoning``
  → ``content`` → ``end`` / ``cancelled``) and the monotonic stamp of
  the last delta, with five optional hooks (``on_load``,
  ``on_lane_shift``, ``on_wait``, ``on_cancel``, ``on_end``). The LOAD
  lane's signal is the ``•••`` typing indicator on the static placeholder draft; a stall over 3 s in the
  reasoning/content lanes fires ``on_wait`` once per stall and
  re-asserts the frame with the current "(Thinking Ns)" value — same
  reasoning, same random_id, the client adopts it in place; the reasoning → content shift fires
  ``on_lane_shift`` before the first content frame goes out
  immediately; a Stop fires ``on_cancel`` and the stream ends with
  ``on_end``. After the final send confirms, a post-delivery callback
  clears the live draft with one ``SendMessageCancelAction`` on the
  sink's resolved peer.

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
import re
import secrets
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Trailing-edge throttle: at most one draft update per interval; the pump
# flushes the latest pending frame when the gate opens.
_DRAFT_MIN_INTERVAL_S = 1.0
# A live draft expires after ~30 s; re-push the current frame well inside it.
_DRAFT_KEEPALIVE_S = 20.0
# Draft payload cap, near Telegram's 4096-char text-draft limit.
_DRAFT_MAX_CHARS = 4000
# Thinking-block cap: reasoning is a preview, not the answer — but the
# preview must keep up with a long thought. 8000 chars (was 2000, ~7-8
# lines, after which the frame froze while the accumulation grew and the
# dedup suppressed every further frame). Combined with the 4000-char
# content cap the richest frame is 12,000 chars, well under the 32,768
# rich ceiling below.
_DRAFT_THINKING_MAX_CHARS = 8000
# Thinking window: the draft shows only the last N reasoning lines — the
# 8th line clears the 1st, the 9th clears the 2nd — so a long thought
# never grows the draft into a big message. The char cap above still
# guards pathological single lines.
_DRAFT_THINKING_WINDOW_LINES = 7
# Thinking prefix: every reasoning draft leads with "(Thinking Ns)",
# N = whole seconds since the first delta arrived. It is decoration,
# not draft content: finalisation strips it before the adoption check.
_THINKING_PREFIX_RE = re.compile(r"^\(Thinking \d+s\) ")
# Rich-draft payload ceiling (mtproto_rich.MAX_RICH_MESSAGE_CHARS):
# Telethon's rich draft action carries no length field of its own, so
# the combined thinking + content build is guarded against this.
_RICH_CEILING_CHARS = 32768
_DRAFT_SEND_TIMEOUT_S = 10.0
_SYNC_DRAFT_TIMEOUT_S = 10.0
# Safety net: the pump never runs past this, even if a turn forgets to stop it.
_PUMP_MAX_LIFETIME_S = 300.0
# Static placeholder text (plain ASCII, no emojis): the LOAD lane primes
# one "Thinking" draft and never changes it — the ••• typing indicator
# on the live draft carries all the motion. The frame cycling that used
# to run here made the prime animation look worse and was removed.
_THINKING_PLACEHOLDER_TEXT = "Thinking"
# A stall with no delta for this long (reasoning/content lanes) fires
# on_wait once per stall and re-asserts the current frame.
_STALL_WAIT_S = 3.0
# Consecutive transient failures before the sink gives up for the turn.
_MAX_CONSECUTIVE_FAILURES = 3
# crc32(b'InputPeer'): a raw request needs a real input peer, never a bare Peer.
_INPUT_PEER_SUBCLASS_ID = 0xC91C90B6


def _fire_hook(callback: Any, *args: Any) -> None:
    """Fire an optional framework hook (on_load, on_lane_shift, on_wait,
    on_cancel, on_end); a dead hook never breaks the turn."""
    if not callable(callback):
        return
    try:
        callback(*args)
    except Exception:
        logger.debug("Streaming response hook failed", exc_info=True)


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


def _tail_frame(markdown: str, limit: int) -> str:
    """Tail window of a draft payload: unchanged under the limit; once it
    exceeds, keep only the newest ``limit`` chars so the draft shows current
    text instead of freezing on the oldest. Fence repair mirrors
    ``_truncate_frame``: a cut inside a code fence re-opens it at the head,
    and an odd fence count in the tail closes it at the end."""
    if limit <= 0:
        return ""
    text = markdown or ""
    if len(text) <= limit:
        if text.count("```") % 2 == 1:
            text = text.rstrip() + "\n```"
        return text
    head, tail = text[:-limit], text[-limit:]
    if head.count("```") % 2 == 1:
        tail = "```\n" + tail
    if tail.count("```") % 2 == 1:
        tail = tail.rstrip() + "\n```"
    return tail


def _last_lines(text: str, count: int) -> str:
    """Last ``count`` newline-separated lines of ``text`` (sliding window).

    A trailing newline is a terminator, not an empty line: it must not
    consume a window slot, or an 8-line thought ending in "\\n" would
    show six lines and a blank."""
    if count <= 0:
        return ""
    body = (text or "").rstrip("\n")
    if not body:
        return ""
    return "\n".join(body.split("\n")[-count:])


def _strip_thinking_prefix(text: str) -> str:
    """Drop a leading "(Thinking Ns) " thinking prefix, if present.

    The prefix is decoration on a reasoning draft, not draft content:
    the sync-draft adoption check compares the final against what the
    draft actually said, so the prefix is stripped before comparing."""
    return _THINKING_PREFIX_RE.sub("", text, count=1)


def _thinking_prefix(
    first_delta_at: Optional[float], time_fn: Callable[[], float]
) -> str:
    """The "(Thinking Ns)" lead for a reasoning draft frame.

    N is whole seconds since the first delta arrived (0 before any
    delta). Computed at frame-build time, so the value updates whenever
    a frame goes out — on a delta, a stall re-assert, or a keepalive —
    with no standalone per-second ticker."""
    if first_delta_at is None:
        seconds = 0
    else:
        seconds = max(0, int(time_fn() - first_delta_at))
    return f"(Thinking {seconds}s)"


def _is_input_peer(peer: Any) -> bool:
    return getattr(peer, "SUBCLASS_OF_ID", None) == _INPUT_PEER_SUBCLASS_ID


def _thinking_block(text: str) -> Any:
    """A Thinking block whose reasoning text renders italic.

    Draft-only: the final message's Reasoning section stays a code block
    (``_hmwa_prepend_reasoning``); italic here is purely the draft's own
    formation. ``TextItalic`` wrapping ``TextPlain`` is a valid ``RichText``
    for ``PageBlockThinking`` (Telethon 1.45.0, layer 233 — verified
    against the installed wheel, not from memory)."""
    from telethon import types

    return types.PageBlockThinking(text=types.TextItalic(text=types.TextPlain(text=text)))


class ResponseStreamDraft:
    """Ephemeral rich streaming preview for one non-guest DM turn.

    Reasoning deltas arrive first and content deltas follow; the draft is
    the cumulative preview — a Thinking block while reasoning-only, then
    the frozen reasoning kept visible above the streaming content, so the
    reasoning shows first and stays through the stream. The draft is only
    a preview — it expires on its own and the complete final message still
    goes out through the normal delivery path, so this sink never declares
    or delivers a final. A Stop press (``can_stop``) halts the draft and
    the generation; the streamed partial is delivered as the final.

    Draft frames are prefix-stable while they fit: reasoning freezes once
    the first content delta arrives (later reasoning would have nowhere to
    go without rewriting earlier text), content only appends, and the
    reasoning frame is a sliding window — "(Thinking Ns)" above the last
    7 lines, the 8th clearing the 1st — so a long thought never grows
    the draft into a big message. Updates are trailing-edge throttled so a
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
        # Thinking text carried by the last sent frame, or None: the client
        # adopts the final only against the draft's leading text, so a last
        # frame that led with a Thinking block always needs a plain sync
        # draft before the final send (see finish_sync_draft).
        self._last_sent_thinking: Optional[str] = None
        self._last_sent_at = 0.0
        self._next_allowed_at = 0.0
        self._pending = False
        self._rich_supported = True
        self._disabled = False
        self._finished = False
        self._stop_requested = False
        # Fired once when a Stop press arrives for this sink's random_id;
        # the runner wires it to interrupt the in-flight generation.
        self.on_stop: Any = None
        # Streaming-control framework hooks (all optional, all None by
        # default, same pattern as on_stop): on_load fires once when the
        # placeholder is primed; on_lane_shift(from, to) on the
        # reasoning -> content shift; on_wait(elapsed) once per stall;
        # on_cancel() when a Stop tears the stream down; on_end(final,
        # stopped) once the stream is over. None may stay unset: on_load's
        # visible signal is the ••• typing indicator on the static
        # placeholder draft, and the sink's own teardown (pump cancel,
        # draft clear) does not depend on any of them.
        self.on_load: Any = None
        self.on_lane_shift: Any = None
        self.on_wait: Any = None
        self.on_cancel: Any = None
        self.on_end: Any = None
        # Control-framework state: the lane this stream is in
        # ("load" | "reasoning" | "content" | "end" | "cancelled"), the
        # monotonic stamp of the last delta (drives the stall wait), and
        # the once-only hook guards.
        self._lane: str = "load"
        self._last_delta_at: Optional[float] = None
        # Monotonic stamp of the FIRST delta: the "(Thinking Ns)" prefix
        # counts whole seconds from here. Set on the first delta of
        # either kind; the prefix only ever renders while reasoning.
        self._first_delta_at: Optional[float] = None
        self._wait_fired = False
        self._load_fired = False
        self._cancel_fired = False
        self._end_fired = False
        self._consecutive_failures = 0
        self._peer: Any = None
        self._pump_future: Any = None
        self._pump_started_at: Optional[float] = None
        self._first_draft_logged = False
        # The static "Thinking" placeholder draft is primed: until the first
        # reasoning or content delta arrives, _frame() offers it unchanged.
        # The ••• typing indicator on the live draft is the motion — the
        # text itself is never cycled or re-sent with different text.
        self._placeholder_primed = False

    @property
    def random_id(self) -> int:
        """The fixed id animating this generation; a Stop press names it."""
        return self._random_id

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def partial_text(self) -> str:
        """Accumulated streamed content, fence-closed — the partial answer
        delivered as the final when the turn is stopped. A stop during
        reasoning (no content yet) delivers the frozen reasoning instead,
        as the final's Reasoning code block: the draft is gone, and
        without this what the user saw would vanish entirely."""
        content = "".join(self._content_parts)
        if content.strip():
            return _truncate_frame(content, 32768)
        reasoning = "".join(self._reasoning_parts).strip()
        if not reasoning:
            return ""
        # Same shape _hmwa_prepend_reasoning renders (header + fenced
        # code block), minus its locale emoji; inner fences are escaped
        # so they cannot break the outer block. Capped like the content
        # path so a very long thought cannot blow up the final.
        return ("**Reasoning:**\n```\n"
                + reasoning[:32768].replace("```", "\\`\\`\\`")
                + "\n```")

    async def clear_draft(self) -> bool:
        """Best-effort post-final draft clear: one SetTypingRequest with
        SendMessageCancelAction on the sink's resolved peer.

        Called after the final send confirms (post-delivery callback),
        never before the sync draft — clearing earlier would kill the
        adoption draft. Only runs when a draft was actually shown.
        Whether the client clears a live draft action on CancelAction
        (vs only the typing indicator) is unverified live; the send is
        logged at INFO so host logs prove whether it went out. The
        adapter task's finally already sends the same cancel via
        stop_typing — this is the peer-correct copy for turns the adapter
        task does not bracket, using the sink's resolved peer rather
        than the cache-only _peer_for. Call on the gateway loop."""
        if self._last_sent_key is None:
            return False
        peer = await self._resolve_input_peer()
        if peer is None:
            return False
        client = getattr(self._adapter, "_client", None)
        if client is None:
            return False
        try:
            from telethon import functions, types

            await asyncio.wait_for(
                client(functions.messages.SetTypingRequest(
                    peer=peer, action=types.SendMessageCancelAction())),
                timeout=_DRAFT_SEND_TIMEOUT_S,
            )
        except Exception:
            logger.debug("Streaming response: draft clear failed for chat %s",
                         self._chat_id, exc_info=True)
            return False
        logger.info("Streaming response draft clear sent: chat=%s", self._chat_id)
        return True

    def request_stop(self) -> None:
        """A Stop press arrived for this sink's random_id: halt the draft
        and the generation. Safe to call from the adapter's event thread:
        flag-setting and pump cancellation are thread-safe, and the
        ``on_stop`` callback (the runner's agent interrupt) is built for
        cross-thread calls. After a Stop the client ignores any further
        draft with this random_id, so the pump must not send again."""
        if self._finished:
            return
        self._stop_requested = True
        self._finished = True
        self._cancel_pump()
        self._lane = "cancelled"
        # INFO, not debug: host logs must prove whether Telegram delivered
        # the Stop update at all — the only unproven link in the stop
        # chain (the rest is wired end to end and covered by tests).
        logger.info("Streaming response Stop press routed: chat=%s random_id=%s",
                    self._chat_id, self._random_id)
        on_stop = self.on_stop
        if callable(on_stop):
            try:
                on_stop()
            except Exception:
                logger.debug("Streaming response stop callback failed", exc_info=True)
        if not self._cancel_fired:
            self._cancel_fired = True
            _fire_hook(self.on_cancel)

    def _frame(self) -> Optional[tuple[str, str]]:
        """(kind, payload) for the current draft, or None when nothing to show.

        A reasoning draft leads with the "(Thinking Ns)" prefix above a
        sliding window of the last 7 reasoning lines: the 8th line clears
        the 1st, the 9th clears the 2nd. The char cap still guards
        pathological single lines."""
        reasoning = "".join(self._reasoning_parts)
        content = "".join(self._content_parts)
        if content:
            text = _tail_frame(content, self._max_chars)
            if not text:
                return None
            return ("rich", text) if self._rich_supported else ("text", text)
        if reasoning:
            windowed = _tail_frame(
                _last_lines(reasoning, _DRAFT_THINKING_WINDOW_LINES),
                _DRAFT_THINKING_MAX_CHARS)
            prefix = _thinking_prefix(self._first_delta_at, self._time_fn)
            text = f"{prefix} {windowed}" if windowed else prefix
            return ("thinking", text)
        if self._placeholder_primed:
            return ("text", _THINKING_PLACEHOLDER_TEXT)
        return None

    def prime_thinking_placeholder(self) -> None:
        """Send the static ``"Thinking"`` placeholder draft that covers the
        typing-to-first-reasoning gap, and fire on_load once.

        Called once at turn setup, right after the sink is created — the
        earliest the streaming sink exists. It is a draft action
        (``SetTypingRequest`` + ``SendMessageTextDraftAction``), never a
        sent message: nothing persists in the chat, and the draft expires
        on its own if the turn produces nothing. The text stays exactly
        ``"Thinking"`` while the turn is still in the LOAD lane — it is
        never cycled; the ••• typing indicator on the live draft carries
        all the motion. The first reasoning or content delta replaces it
        (``_frame`` stops offering the placeholder once anything has
        arrived), and the pump's keepalive re-pushes the identical frame
        inside the ~30 s draft TTL while the turn is still thinking.
        Streaming turns only: the sink exists only for a DM turn with the
        Beta setting on, so a non-streaming turn never primes one. The
        placeholder carries the sink's fixed random_id, so a Stop press
        during the gap still routes to this sink."""
        if self._disabled or self._finished or self._placeholder_primed:
            return
        self._placeholder_primed = True
        if not self._load_fired:
            self._load_fired = True
            _fire_hook(self.on_load)
        self._maybe_send()

    def on_reasoning(self, text: str) -> None:
        if not text or self._content_parts:
            return
        first = not self._reasoning_parts
        self._reasoning_parts.append(text)
        self._last_delta_at = self._time_fn()
        if self._first_delta_at is None:
            self._first_delta_at = self._last_delta_at
        self._wait_fired = False
        if first:
            # LOAD -> REASONING: the first real frame leaves the LOAD lane
            # immediately — a lane exit never waits behind the trailing-edge
            # throttle gate, or the gate would hold the first reasoning
            # frame up to a second.
            self._lane = "reasoning"
            if not self._disabled and not self._finished:
                frame = self._frame()
                if frame is not None:
                    self._send_now(frame)
                return
        self._maybe_send()

    def on_content(self, text: str) -> None:
        if not text:
            return
        first = not self._content_parts
        had_reasoning = bool(self._reasoning_parts)
        self._content_parts.append(text)
        self._last_delta_at = self._time_fn()
        if self._first_delta_at is None:
            self._first_delta_at = self._last_delta_at
        self._wait_fired = False
        if first and had_reasoning:
            # REASONING -> CONTENT: the visible lane shift. Fire the hook
            # BEFORE sending, and send this first content frame immediately
            # via _send_now, bypassing the 1.0 s trailing-edge gate — held
            # behind it, the frame would flush a second later as a sudden
            # coalesced jump, and the switch would look frozen.
            self._lane = "content"
            _fire_hook(self.on_lane_shift, "reasoning", "content")
            if not self._disabled and not self._finished:
                frame = self._frame()
                if frame is not None:
                    self._send_now(frame)
                return
        if first and not had_reasoning:
            # LOAD -> CONTENT: no reasoning ever arrived; the first content
            # frame leaves the LOAD lane immediately, same as the first
            # reasoning frame above.
            self._lane = "content"
            if not self._disabled and not self._finished:
                frame = self._frame()
                if frame is not None:
                    self._send_now(frame)
                return
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
                sent_this_tick = False
                # No LOAD-lane animation branch: the placeholder is one
                # static "Thinking" draft, primed once — the ••• typing
                # indicator is the motion, and re-sending the text with
                # more dots made the prime animation look worse. The
                # keepalive below still re-pushes the identical frame
                # inside the draft TTL.
                if self._pending and now >= self._next_allowed_at and now - self._last_sent_at >= self._min_interval_s:
                    self._pending = False
                    self._last_sent_at = now
                    await self._send_frame(*frame)
                    sent_this_tick = True
                elif (self._last_sent_key is not None
                        and now - self._last_sent_at >= self._keepalive_s):
                    self._last_sent_at = now
                    await self._send_frame(*frame)
                    sent_this_tick = True
                if (not sent_this_tick and self._lane in ("reasoning", "content")
                        and self._last_delta_at is not None):
                    elapsed = now - self._last_delta_at
                    if elapsed > _STALL_WAIT_S and not self._wait_fired:
                        # Stall: no delta for 3 s. Fire on_wait once per
                        # stall and re-assert the current frame directly,
                        # so the draft bubble visibly refreshes after any
                        # overwrite. The re-asserted frame carries the
                        # current thinking seconds, so it is not byte-identical
                        # to the stalled frame — same random_id, so the
                        # client still adopts it in place. The next delta
                        # clears _wait_fired for the next stall.
                        self._wait_fired = True
                        _fire_hook(self.on_wait, elapsed)
                        frame = self._frame()
                        if frame is not None:
                            await self._send_frame(*frame)
                        sent_this_tick = True
        except asyncio.CancelledError:
            return

    def finish_sync_draft(self, final_text: str) -> None:
        """Push one draft equal to the first chunk of the completed final, awaited.

        Called on the worker thread just before the final send. The client
        adopts the final only when it shares a leading prefix with the
        last draft's text; when the last draft already starts the final
        there is nothing to sync. Always stops the pump afterwards, so no
        ghost draft can land after the final message. A stopped sink still
        syncs: ``request_stop`` already cancelled the pump, so this one
        awaited send is the last draft by construction. Fires on_end once
        the stream is over, on every exit path.
        """
        if self._finished and not self._stop_requested:
            return
        self._finished = True
        self._cancel_pump()
        try:
            if self._disabled or not final_text or not str(final_text).strip():
                return
            if self._last_sent_key is None:
                return  # never showed a draft; nothing to sync
            # The sync draft stays a PREFIX cut even though live frames are
            # tail windows: the client adopts the final only when it shares
            # a leading prefix with the last draft. A tail-windowed last
            # draft never matches the 8-char skip below, so the sync is
            # always sent — and it must be the first chunk of the final.
            first = _truncate_frame(str(final_text), self._max_chars)
            if not first:
                return
            last_text = self._last_sent_key[1]
            # The thinking prefix is decoration, not draft content: the
            # adoption rule compares the final against what the draft
            # actually said, so strip a leading "(Thinking Ns) " before
            # the prefix check. A last frame that led with a Thinking
            # block always needs a plain sync draft anyway
            # (_last_sent_thinking forces it below), so the strip only
            # keeps the comparison honest — it never skips a sync the
            # thinking flag would have sent.
            compare_text = _strip_thinking_prefix(last_text)
            prefix_len = min(8, len(compare_text), len(first))
            if (compare_text and first and prefix_len > 0
                    and compare_text[:prefix_len] == first[:prefix_len]
                    and not self._last_sent_thinking):
                return
            kind = "rich" if self._rich_supported else "text"
            self._send_sync(kind, first)
        finally:
            if not self._end_fired:
                self._end_fired = True
                self._lane = "end"
                _fire_hook(self.on_end, final_text, self._stop_requested)

    def _send_sync(self, kind: str, text: str) -> None:
        """Schedule one draft frame and block (bounded) until it lands or fails.

        The sync draft carries no Stop button and no Thinking block: the
        turn is over, and the draft must be the plain final chunk so the
        client adopts the final."""
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("Streaming response sync draft dropped: no event loop on this thread")
                return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_frame(kind, text, can_stop=False, include_thinking=False), loop)
        except RuntimeError:
            logger.debug("Streaming response sync draft dropped: event loop is closed", exc_info=True)
            return
        try:
            future.result(timeout=_SYNC_DRAFT_TIMEOUT_S)
        except Exception:
            logger.debug("Streaming response sync draft did not complete in time", exc_info=True)

    def cancel(self) -> None:
        """Stop the pump and fire the teardown hooks; the live draft is
        cleared by the post-delivery clear once the final lands, or
        expires on its own if the turn never delivers. A Stop-driven
        cancel does not re-fire on_cancel (request_stop already did)."""
        was_stop = self._stop_requested
        self._finished = True
        self._cancel_pump()
        self._lane = "cancelled"
        if not was_stop and not self._cancel_fired:
            self._cancel_fired = True
            _fire_hook(self.on_cancel)
        if not self._end_fired:
            self._end_fired = True
            _fire_hook(self.on_end, None, was_stop)

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

    def _build_action(self, kind: str, text: str, *, can_stop: bool = True,
                        include_thinking: bool = True) -> Any:
        """Draft action for one frame. ``can_stop`` shows the native Stop
        button; ``keep_on_stop`` leaves the partial draft visible after
        Stop until the real message replaces it. The sync draft (turn over)
        passes ``can_stop=False`` and ``include_thinking=False`` — it must
        be the plain final chunk so the client adopts the final.

        A rich frame keeps the frozen reasoning visible above the content
        (Thinking block + content blocks in one rich draft), so the
        reasoning shows first and is never lost once content flows. If the
        combined build fails, fall back to the plain Markdown rich frame —
        a dead preview must never break the turn.

        Live frames carry the green ``autonomous`` tag (native button row
        at index 0) while the mode is active with badges on; the gate is
        read per frame and fails open to untagged. The sync draft
        (``can_stop=False``, ``include_thinking=False``) never carries it:
        it must stay the plain final chunk so the client adopts the final.
        Text frames cannot carry a button row and stay untagged."""
        from telethon import types

        from gateway import autonomous_state

        tag = (can_stop and include_thinking
               and autonomous_state.autonomous_tag_enabled(self._adapter))

        if kind == "thinking":
            from .mtproto_rich import autonomous_tag_block_row

            blocks = ([autonomous_tag_block_row()] if tag else []) + [_thinking_block(text)]
            rich = types.InputRichMessage(blocks=blocks)
            return types.InputSendMessageRichMessageDraftAction(
                rich_message=rich, random_id=self._random_id,
                can_stop=can_stop, keep_on_stop=True)
        if kind == "rich":
            from .mtproto_rich import (
                autonomous_tag_block_row, input_rich_markdown, input_rich_message)

            reasoning = "".join(self._reasoning_parts)
            if reasoning and include_thinking:
                try:
                    blocks_msg = input_rich_message(text, rich_format="blocks")
                    # Combined payload guard: the frozen reasoning plus the
                    # content must stay under the rich ceiling. At the
                    # defaults this is 8000 + 4000 = 12,000; the min()
                    # only bites if a caller raises max_chars past it.
                    # The frozen reasoning is the same 7-line window the
                    # thinking frame showed — no prefix here: the agent
                    # is already responding with actual content.
                    room = max(0, _RICH_CEILING_CHARS - len(text))
                    thinking_text = _tail_frame(
                        _last_lines(reasoning, _DRAFT_THINKING_WINDOW_LINES),
                        min(_DRAFT_THINKING_MAX_CHARS, room))
                    combined = types.InputRichMessage(blocks=(
                        ([autonomous_tag_block_row()] if tag else [])
                        + [_thinking_block(thinking_text), *blocks_msg.blocks]
                    ))
                    return types.InputSendMessageRichMessageDraftAction(
                        rich_message=combined, random_id=self._random_id,
                        can_stop=can_stop, keep_on_stop=True)
                except Exception:
                    logger.debug("Streaming response: combined rich draft failed; "
                                 "falling back to Markdown rich", exc_info=True)
            if tag:
                # Content-only live frame: the Markdown payload cannot
                # carry a native row, so it rides the blocks route (the
                # same conversion the copyable-code auto-route applies)
                # with the tag at 0.
                rich_message = input_rich_message(text, rich_format="blocks",
                                                  autonomous_tag=True)
            else:
                rich_message = input_rich_markdown(text)
            return types.InputSendMessageRichMessageDraftAction(
                rich_message=rich_message, random_id=self._random_id,
                can_stop=can_stop, keep_on_stop=True)
        return types.SendMessageTextDraftAction(
            text=types.TextWithEntities(text=text, entities=[]),
            random_id=self._random_id,
            can_stop=can_stop, keep_on_stop=True)

    async def _send_frame(self, kind: str, text: str, *, _retry_peer: bool = True,
                        can_stop: bool = True, include_thinking: bool = True) -> bool:
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
            action = self._build_action(kind, text, can_stop=can_stop,
                                         include_thinking=include_thinking)
        except Exception:
            # A frame that cannot even be built must not vanish silently:
            # keep it pending so the pump retries it, and count the
            # failure like every other transient failure (FloodWait and
            # Timeout do exactly this). Without the pending flag the first
            # content frame was lost until the next delta or the 20 s
            # keepalive.
            logger.debug("Streaming response: could not build draft action", exc_info=True)
            self._pending = True
            return self._note_failure()
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
                return await self._send_frame(kind, text, _retry_peer=False, can_stop=can_stop,
                                             include_thinking=include_thinking)
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
                return await self._send_frame("text", text, _retry_peer=False, can_stop=can_stop,
                                             include_thinking=include_thinking)
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
        self._last_sent_thinking = (
            "".join(self._reasoning_parts)
            if kind == "thinking" or (kind == "rich" and include_thinking and self._reasoning_parts)
            else None
        )
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
