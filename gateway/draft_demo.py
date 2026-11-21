"""Live draft demos for the ``/draft`` command (topics 2, 6 and 8).

The demos showcase the real ``ResponseStreamDraft`` sink
(``plugins/platforms/telegram/streaming_response.py``): every demo frame
is a draft action (``SetTypingRequest``), never a sent message, so nothing
persists in the chat and the draft expires on its own. Each demo builds
its OWN sink with its OWN ``random_id`` — never the live turn's sink —
registered in ``adapter._streaming_sinks`` exactly like
``TelethonAdapter.streaming_response_sink`` does, so a Stop press routes
to it. The demos deliberately do NOT go through
``streaming_response_sink()``: that factory is gated on the profile-wide
streaming setting, and the demo must work even with streaming off.

DM-only: Telegram drafts are private 1-1 only, so a non-DM source
returns False without creating a sink.

Every animation holds 10 s or more so the user can actually watch it.
A plain typing action expires on the client after ~5 s, so the raw
action steps re-send their action every 4 s inside the hold; the draft
actions are re-pushed by the sink pump on the same cadence. The
``/draft`` handler previews each step with :func:`preview_next` before
running it (a one-line label — the preview stays at 1.5 s, long enough
to read), then sends a real persist message with the animation's
content via :func:`send_animation_message`, so every animation stays
visible after its draft expires.

Full action coverage: the demo covers EVERY ``SendMessage*Action``
the installed Telethon wheel (1.45.0) knows — the three draft actions
(``TextDraft``, ``RichMessageDraft``, ``StopDraft``) through the sink
steps, and the other fifteen through :func:`show_action` /
:func:`show_stop_draft` (see :data:`NON_DRAFT_ACTIONS` /
:data:`DRAFT_ACTION_NAMES`). ``SendMessageAction`` subtypes that are
not typing statuses demoable in a DM are out of scope by design:
``SpeakingInGroupCallAction`` only means something inside a group
call, and the emoji interactions need a target message plus an
interaction payload. ``run_demo_topic2`` / ``run_demo_topic6`` are thin
wrappers over the steps, kept for the original two demos.
"""
import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Sample texts, all clearly marked as demo material so a screenshot can
# never be mistaken for a real answer.
DEMO_REASONING_TOPIC2 = (
    "Demo reasoning: working out how a live draft preview looks, step by step."
)
DEMO_CONTENT_TOPIC2 = (
    "Demo content: this is the streamed answer preview — it arrives after "
    "the reasoning, with the reasoning frozen above it."
)
DEMO_FINAL_TOPIC2 = (
    "Demo complete — the live draft preview finished and this is the final message."
)
DEMO_REASONING_TOPIC6 = (
    "Demo reasoning: this text exists only in the draft — it vanishes when "
    "the draft expires and never reaches a final message."
)

# Every animation holds 10 s or more. A typing action expires on the
# client after ~5 s, so raw action steps re-send every 4 s inside the hold.
_ACTION_HOLD_S = 10.0
_ACTION_RESEND_S = 4.0
# One raw action send never blocks the demo longer than this.
_ACTION_SEND_TIMEOUT_S = 10.0

# Draft actions, demoed by the sink steps below (placeholder, reasoning,
# content, sync, temporary reasoning, stop-draft).
DRAFT_ACTION_NAMES: tuple[str, ...] = (
    "SendMessageTextDraftAction",
    "SendMessageRichMessageDraftAction",
    "SendMessageStopDraftAction",
)

# The other SendMessage*Action types the installed Telethon wheel (1.45.0)
# knows: (TL type name, what the peer's client shows — the official
# core.telegram.org description). Each is demoed by show_action, one beat
# per action, and persisted by naming it. Progress actions upload at 50%.
NON_DRAFT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("SendMessageTypingAction", "User is typing."),
    ("SendMessageUploadPhotoAction", "User is uploading a photo."),
    ("SendMessageUploadVideoAction", "User is uploading a video."),
    ("SendMessageUploadAudioAction", "User is uploading a voice message."),
    ("SendMessageUploadDocumentAction", "User is uploading a file."),
    ("SendMessageUploadRoundAction", "User is uploading a round video."),
    ("SendMessageRecordVideoAction", "User is recording a video."),
    ("SendMessageRecordAudioAction", "User is recording a voice message."),
    ("SendMessageRecordRoundAction", "User is recording a round video to share."),
    ("SendMessageGeoLocationAction", "User is selecting a location to share."),
    ("SendMessageChooseContactAction", "User is selecting a contact to share."),
    ("SendMessageChooseStickerAction", "User is choosing a sticker."),
    ("SendMessageGamePlayAction", "User is playing a game."),
    ("SendMessageHistoryImportAction", "Chat history is being imported."),
    ("SendMessageCancelAction",
     "Invalidate all previous action updates; the indicator clears."),
)

# Raw actions carrying a progress percentage (0-100).
_PROGRESS_ACTIONS = frozenset({
    "SendMessageHistoryImportAction",
    "SendMessageUploadAudioAction",
    "SendMessageUploadDocumentAction",
    "SendMessageUploadPhotoAction",
    "SendMessageUploadRoundAction",
    "SendMessageUploadVideoAction",
})


def build_action(name: str) -> Any:
    """Build one raw ``SendMessage*Action`` by TL type name.

    Progress actions upload at 50%. The two payload drafts
    (``TextDraft``, ``RichMessageDraft``) cannot be built bare — the
    sink builds them with their payload — so they raise ValueError,
    as does any name the wheel does not know.
    """
    from telethon import types

    if name in ("SendMessageTextDraftAction", "SendMessageRichMessageDraftAction"):
        raise ValueError(f"{name} needs a payload; built by the sink, never bare")
    cls = getattr(types, name, None)
    if cls is None:
        raise ValueError(f"unknown SendMessage action: {name}")
    if name in _PROGRESS_ACTIONS:
        return cls(progress=50)
    return cls()


def _is_dm(source: Any) -> bool:
    return str(getattr(source, "chat_type", "")) == "dm"


def _build_sink(adapter: Any, source: Any, loop: Any) -> Any:
    """Fresh demo sink, registered for Stop routing like the turn sink.

    Prunes finished sinks, then registers by ``random_id`` — the same
    shape as ``TelethonAdapter.streaming_response_sink``.
    """
    from plugins.platforms.telegram.streaming_response import ResponseStreamDraft

    sink = ResponseStreamDraft(adapter, str(source.chat_id), loop=loop)
    adapter._streaming_sinks = {
        rid: s for rid, s in getattr(adapter, "_streaming_sinks", {}).items()
        if not getattr(s, "_finished", False)
    }
    adapter._streaming_sinks[sink.random_id] = sink
    return sink


def _sink_dead(sink: Any) -> bool:
    """True when the sink gave up (e.g. peer unresolvable): no draft can
    ever be shown through it, so the demo cannot run."""
    return bool(getattr(sink, "_disabled", False))


async def _run_step(adapter: Any, source: Any, *, loop: Any = None,
                    sleep: Any = asyncio.sleep, body: Any) -> bool:
    """Shared guard + sink lifecycle for one demo step.

    DM check, live-client check and loop resolution run before any sink
    exists; ``body`` receives ``(sink, sleep, is_stopped)`` and returns
    True when the step ran to completion. The sink is cancelled in
    ``finally`` so the live draft expires on its own. Any exception in
    ``body`` fails the step as False.
    """
    if not _is_dm(source):
        return False
    if getattr(adapter, "_client", None) is None:
        return False
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
    sink = _build_sink(adapter, source, loop)
    stopped = False

    def _on_stop() -> None:
        nonlocal stopped
        stopped = True

    sink.on_stop = _on_stop
    try:
        return bool(await body(sink, sleep, lambda: stopped))
    except Exception:
        logger.debug("Draft demo step failed", exc_info=True)
        return False
    finally:
        # Stop the pump; the live draft expires on its own.
        sink.cancel()


async def preview_next(adapter: Any, source: Any, text: str, *, loop: Any = None,
                       sleep: Any = asyncio.sleep, hold_delay: float = 1.5) -> bool:
    """Streaming draft preview naming the next message: a live rich draft
    reading exactly ``text`` (e.g. ``"Next: draft actions…"``), held, then
    cancelled. Sends no message. True if the preview showed, False if it
    could not run or was stopped.
    """
    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        # A rich draft of the plain preview text; no new sink method —
        # on_content renders plain text cleanly as a rich draft.
        sink.on_content(text)
        await _sleep(hold_delay)
        if is_stopped() or _sink_dead(sink):
            return False
        return True

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def show_placeholder(adapter: Any, source: Any, *, loop: Any = None,
                           sleep: Any = asyncio.sleep, hold_delay: float = 10.0) -> bool:
    """Static ``"Thinking"`` placeholder text draft covering the typing gap, held,
    then cancelled. Sends no message. True if it showed, False if it could
    not run or was stopped.
    """
    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        sink.prime_thinking_placeholder()
        await _sleep(hold_delay)
        if is_stopped() or _sink_dead(sink):
            return False
        return True

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def show_reasoning(adapter: Any, source: Any, text: str, *, loop: Any = None,
                         sleep: Any = asyncio.sleep, hold_delay: float = 10.0) -> bool:
    """Italic reasoning draft (Thinking block, draft-only), held, then
    cancelled. Sends no message. True if it showed, False if it could not
    run or was stopped.
    """
    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        sink.on_reasoning(text)
        await _sleep(hold_delay)
        if is_stopped() or _sink_dead(sink):
            return False
        return True

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def show_content(adapter: Any, source: Any, text: str, *, loop: Any = None,
                       sleep: Any = asyncio.sleep, hold_delay: float = 10.0) -> bool:
    """Content draft with the frozen reasoning above it, as in the real
    stream: the reasoning frame first, then the content frame carrying
    the same reasoning as a Thinking block. Held, then cancelled. Sends
    no message. True if it showed, False if it could not run or was
    stopped.
    """
    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        sink.on_reasoning(DEMO_REASONING_TOPIC2)
        sink.on_content(text)
        await _sleep(hold_delay)
        if is_stopped() or _sink_dead(sink):
            return False
        return True

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def show_sync_and_final(adapter: Any, source: Any, final_text: str, *,
                              loop: Any = None,
                              sleep: Any = asyncio.sleep,
                              prime_delay: float = 10.0) -> bool:
    """Sync draft, then the real final message through the normal delivery
    path. The fresh sink first shows a reasoning frame (the demo
    reasoning) and holds for ``prime_delay`` so the frame lands and sets
    the sink's last sent frame; only then can ``finish_sync_draft`` push
    the plain sync draft — the first chunk of the final, no Stop button —
    before the final send. Without the prime the sync is a no-op (the
    sink never showed a draft) and the animation is missing. Returns the
    final send's success; False if it could not run, was stopped, the
    sink died, or the send failed.
    """
    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        # Prime first: a Thinking-led last frame always needs a plain
        # sync draft before the final (see finish_sync_draft), and the
        # prime is what gives the fresh sink a last frame to sync
        # against. The hold lets the frame land on the wire — the sync
        # call below only sends when the last frame is already set.
        sink.on_reasoning(DEMO_REASONING_TOPIC2)
        await _sleep(prime_delay)
        if is_stopped() or _sink_dead(sink):
            return False
        # The sync call blocks (bounded wait on the loop), so it runs
        # off-loop exactly like the turn-final path, which calls it from
        # the worker thread.
        await asyncio.to_thread(sink.finish_sync_draft, final_text)
        if is_stopped() or _sink_dead(sink):
            return False
        result = await adapter.send(str(source.chat_id), final_text)
        return bool(getattr(result, "success", False))

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def send_animation_message(adapter: Any, source: Any, text: str) -> bool:
    """Persist one animation as a real plain-text message, via the
    adapter's plain send — the same path as the demo final message.
    DM-only with a live-client guard. True when the send succeeded,
    False otherwise (the handler aborts the rest of the sequence).
    """
    if not _is_dm(source):
        return False
    if getattr(adapter, "_client", None) is None:
        return False
    try:
        result = await adapter.send(str(source.chat_id), text)
    except Exception:
        logger.debug("Draft demo persist message failed", exc_info=True)
        return False
    return bool(getattr(result, "success", False))


async def _send_raw_action(adapter: Any, peer: Any, action: Any) -> bool:
    """One raw typing action via SetTypingRequest — not a draft frame.
    False on any failure (a rejected action fails its step cleanly)."""
    client = getattr(adapter, "_client", None)
    if client is None:
        return False
    try:
        from telethon import functions

        await asyncio.wait_for(
            client(functions.messages.SetTypingRequest(peer=peer, action=action)),
            timeout=_ACTION_SEND_TIMEOUT_S,
        )
    except Exception:
        logger.debug("Draft demo action send failed", exc_info=True)
        return False
    return True


async def show_action(adapter: Any, source: Any, action_name: str, *,
                      loop: Any = None, sleep: Any = asyncio.sleep,
                      hold_delay: float = _ACTION_HOLD_S,
                      resend_interval: float = _ACTION_RESEND_S) -> bool:
    """One raw typing action, held for the full hold: sent at once, then
    re-sent every ``resend_interval`` — a single send expires on the
    client after ~5 s, so without the re-sends the indicator would
    vanish mid-hold. Sends no message. True if it ran the whole hold,
    False if it could not run, was stopped, or a send failed.
    """
    try:
        action = build_action(action_name)
    except Exception:
        # Unknown name: fail before any sink exists.
        logger.debug("Draft demo action name unknown: %s", action_name)
        return False

    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        peer = await sink._resolve_input_peer()
        if peer is None:
            return False
        remaining = hold_delay
        while True:
            if not await _send_raw_action(adapter, peer, action):
                return False
            if is_stopped() or _sink_dead(sink):
                return False
            if remaining <= resend_interval:
                await _sleep(remaining)
                break
            await _sleep(resend_interval)
            remaining -= resend_interval
            if is_stopped() or _sink_dead(sink):
                return False
        return not (is_stopped() or _sink_dead(sink))

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def show_stop_draft(adapter: Any, source: Any, *, loop: Any = None,
                          sleep: Any = asyncio.sleep,
                          hold_delay: float = _ACTION_HOLD_S,
                          resend_interval: float = _ACTION_RESEND_S) -> bool:
    """Show the demo reasoning draft, then clear it with
    ``SendMessageStopDraftAction`` naming this sink's ``random_id`` —
    re-sent so a late pump frame cannot resurrect the draft inside the
    hold. Sends no message. True if the draft showed and the stop ran
    the whole hold, False if it could not run or was stopped.
    """
    async def _body(sink: Any, _sleep: Any, is_stopped: Any) -> bool:
        from telethon import types

        peer = await sink._resolve_input_peer()
        if peer is None:
            return False
        stop = types.SendMessageStopDraftAction(random_id=sink.random_id)
        sink.on_reasoning(DEMO_REASONING_TOPIC2)
        await _sleep(resend_interval)  # let the draft land first
        if is_stopped() or _sink_dead(sink):
            return False
        remaining = hold_delay - resend_interval
        while True:
            if not await _send_raw_action(adapter, peer, stop):
                return False
            if is_stopped() or _sink_dead(sink):
                return False
            if remaining <= resend_interval:
                await _sleep(remaining)
                break
            await _sleep(resend_interval)
            remaining -= resend_interval
            if is_stopped() or _sink_dead(sink):
                return False
        return not (is_stopped() or _sink_dead(sink))

    return await _run_step(adapter, source, loop=loop, sleep=sleep, body=_body)


async def run_demo_topic2(adapter: Any, source: Any, *, loop: Any = None,
                          sleep: Any = asyncio.sleep,
                          frame_delay: float = 10.0) -> bool:
    """Live draft sequence: placeholder -> reasoning -> content -> sync + final message.

    Thin wrapper over the steps, one fresh sink per step. Sends the final
    message itself (via the adapter's plain-message ``send``). True if it
    ran and delivered, False if it could not run or was stopped.
    """
    if not await show_placeholder(
            adapter, source, loop=loop, sleep=sleep, hold_delay=frame_delay):
        return False
    if not await show_reasoning(
            adapter, source, DEMO_REASONING_TOPIC2, loop=loop, sleep=sleep,
            hold_delay=frame_delay):
        return False
    if not await show_content(
            adapter, source, DEMO_CONTENT_TOPIC2, loop=loop, sleep=sleep,
            hold_delay=frame_delay):
        return False
    return await show_sync_and_final(
        adapter, source, DEMO_FINAL_TOPIC2, loop=loop, sleep=sleep)


async def run_demo_topic6(adapter: Any, source: Any, *, loop: Any = None,
                          sleep: Any = asyncio.sleep,
                          show_delay: float = 10.0) -> bool:
    """Temporary reasoning demo: shows an italic reasoning draft, then cancels the sink.

    Thin wrapper over :func:`show_reasoning`. Sends NO message itself —
    the caller sends the explanatory card after this returns True. True
    if the reasoning draft was shown, False if it could not run or was
    stopped.
    """
    return await show_reasoning(
        adapter, source, DEMO_REASONING_TOPIC6, loop=loop, sleep=sleep,
        hold_delay=show_delay)
