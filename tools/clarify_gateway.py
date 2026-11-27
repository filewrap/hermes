"""Gateway-side clarify primitive (blocking event-based queue). The agent runs on a worker
thread while the event loop handles the user's reply, so a pending clarify is stored
module-level (same shape as ``tools.approval``) and the agent thread blocks on an ``Event``
until an adapter button callback or the gateway text-intercept resolves it, or the timeout
fires. Adapters render inline buttons (an "Other" row flips the entry into text-capture
mode) or a numbered-list text fallback."""

from __future__ import annotations
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class _ClarifyEntry:
    """One pending clarify request inside a gateway session."""
    clarify_id: str
    session_key: str
    question: str
    choices: Optional[list[str]]
    multi_select: bool = False
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None
    awaiting_text: bool = False  # set when user picked "Other" or clarify is open-ended
    # Telegram user id the question was asked to (the turn's triggering user); None when
    # the registration carried no identity (e.g. command-choice prompts). Poll delivery
    # enforces it: only this user may answer. Compared as strings; never inferred.
    respondent_id: Optional[str] = None
    # Who may answer it from a chat (``gateway.approval_actor.clarify_refusal``).
    authority: Optional[dict] = None


@dataclass
class _ClarifyPoll:
    """A clarify delivered as a native Telegram poll: the poll id a vote update
    carries is the only key back to the clarify, so it is tracked separately
    from the entry — the entry is popped by ``wait_for_response`` before the
    runner retires the card, and retire still has to find (and stop) the poll."""
    clarify_id: str
    poll_id: int
    message_id: int
    chat_id: str
    thread_id: str = ""


_lock = threading.RLock()
_entries: dict[str, _ClarifyEntry] = {}  # clarify_id -> entry (button callbacks)
_session_index: dict[str, list[str]] = {}  # session_key -> [clarify_id] FIFO (text intercept, cleanup)
# Per-session notify callbacks (gateway -> adapter bridge); mirrors tools.approval. Tests clear it.
_notify_cbs: dict[str, Callable[[_ClarifyEntry], None]] = {}
# Telegram clarify polls: clarify_id -> poll, and poll_id -> clarify_id. A poll id is
# the only thing a vote update carries, so both directions are indexed.
_polls: dict[str, _ClarifyPoll] = {}
_poll_index: dict[int, str] = {}

# Outcomes for typed clarify replies. Gateway cancels the pending prompt on
# free prose (deadlock break) but keeps it armed for a retryable bad selection.
TEXT_RESOLVED = "resolved"
TEXT_REJECTED_PROSE = "rejected_prose"
TEXT_REJECTED_SELECTION = "rejected_selection"
TEXT_NO_PENDING = "no_pending"
SKIP_WORD = "skip"
SKIPPED = "\x00skipped"
CANCELLED = "\x00cancelled"


def register(clarify_id: str, session_key: str, question: str, choices: Optional[list[str]],
             multi_select: bool = False, respondent_id: Optional[str] = None,
             authority: Optional[dict] = None) -> _ClarifyEntry:
    """Register a pending clarify request; caller then blocks on ``wait_for_response``.
    Open-ended (no choices) entries start in text mode: the next message IS the response.
    ``respondent_id`` is the Telegram user id the question was asked to; only that user
    may answer a poll-delivered clarify (None = no enforcement, as before).
    *authority* says who may answer it; default: the current turn's actor, approvers and admins."""
    if authority is None:
        from tools.approval_context import prompt_authority
        authority = prompt_authority()
    entry = _ClarifyEntry(clarify_id, session_key, question, list(choices) if choices else None,
                          bool(multi_select) and bool(choices), awaiting_text=not bool(choices),
                          respondent_id=str(respondent_id) if respondent_id is not None else None,
                          authority=authority)
    with _lock:
        _entries[clarify_id] = entry
        _session_index.setdefault(session_key, []).append(clarify_id)
    return entry


def register_clarify_poll(clarify_id: str, poll_id: int, message_id: int, chat_id: str,
                          thread_id: str = "") -> Optional[_ClarifyPoll]:
    """Track a clarify delivered as a Telegram poll. Returns None (and records nothing)
    when the clarify is unknown — a poll must never outlive its entry at creation time."""
    with _lock:
        if clarify_id not in _entries:
            return None
        poll = _ClarifyPoll(clarify_id, int(poll_id), int(message_id), str(chat_id),
                            str(thread_id or ""))
        _polls[clarify_id] = poll
        _poll_index[poll.poll_id] = clarify_id
        return poll


def get_clarify_poll(clarify_id: str) -> Optional[_ClarifyPoll]:
    """The poll a clarify was delivered as, or None when it was not poll-delivered."""
    with _lock:
        return _polls.get(clarify_id)


def get_clarify_poll_for_poll_id(poll_id: int) -> Optional[tuple[_ClarifyPoll, Optional[_ClarifyEntry]]]:
    """Map a vote update's poll id back to its poll and pending entry. Returns None when
    the poll id was never a clarify poll. The entry is None when it already resolved or
    expired — the poll record is still returned so the caller can stop the stale poll
    instead of leaving it open."""
    with _lock:
        clarify_id = _poll_index.get(int(poll_id))
        if clarify_id is None:
            return None
        poll = _polls.get(clarify_id)
        if poll is None:
            return None
        entry = _entries.get(clarify_id)
        if entry is not None and entry.event.is_set():
            entry = None
        return poll, entry


def drop_clarify_poll(clarify_id: str) -> bool:
    """Forget a clarify's poll (after it was stopped, or when the clarify is gone);
    False when nothing was tracked."""
    with _lock:
        poll = _polls.pop(clarify_id, None)
        if poll is None:
            return False
        _poll_index.pop(poll.poll_id, None)
        return True


def wait_for_response(clarify_id: str, timeout: float) -> Optional[str]:
    """Block until the entry resolves or ``timeout`` (``<= 0`` = unlimited) elapses; None on
    timeout/unknown id. Polls in 1s slices so the inactivity heartbeat keeps firing (a
    single long ``Event.wait`` would let the gateway watchdog kill a live prompt)."""
    with _lock:
        entry = _entries.get(clarify_id)
    if entry is None:
        return None
    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - optional
        touch_activity_if_due = None
    from tools.interrupt import is_interrupted
    deadline = None if timeout is None or float(timeout) <= 0.0 else time.monotonic() + float(timeout)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        remaining = 1.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0 or entry.event.wait(timeout=min(1.0, remaining)):
            break
        if is_interrupted():
            # /stop or gateway shutdown on this turn's thread: withdraw the prompt the way the
            # approval wait does, or a planned stop sits out the whole clarify timeout and the
            # turn is left started (unknown after restart). A real answer that won the race stays.
            with _lock:
                if not entry.event.is_set():
                    entry.response = CANCELLED
            break
        # Periodic activity touch so the gateway's inactivity timeout doesn't kill the agent during long
        # code execution (#10807).
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for user clarify response")
    with _lock:
        _entries.pop(clarify_id, None)  # regardless of outcome
        entry.event.set()  # Timeout also retires shared projections of this waiter.
        ids = _session_index.get(entry.session_key) or []
        if clarify_id in ids:
            ids.remove(clarify_id)
            if not ids:
                _session_index.pop(entry.session_key, None)
    # The wake is the hop the host log could never see: an intercepted answer
    # sets the event, but nothing said whether the waiting turn ever received
    # it. Log the outcome (never the answer text itself).
    if entry.response is None:
        logger.info("Clarify wait for %s timed out after %ss with no answer",
                    clarify_id, timeout)
    elif entry.response == CANCELLED:
        logger.info("Clarify wait for %s ended cancelled", clarify_id)
    elif entry.response == "":
        logger.info("Clarify wait for %s returned an empty answer", clarify_id)
    else:
        logger.info("Clarify wait for %s returned an answer", clarify_id)
    return entry.response


def resolve_gateway_clarify(clarify_id: str, response: str) -> bool:
    """Unblock the waiter on ``clarify_id``; False if already resolved/expired/unknown."""
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None or entry.event.is_set():
            return False
        entry.response = str(response) if response is not None else ""
        entry.event.set()
        return True


def get_pending(clarify_id: str) -> Optional[_ClarifyEntry]:
    """The entry for ``clarify_id`` while it is still awaiting an answer, else None (unknown,
    answered, timed out or cleared). Read-only: adapters rendering native choice cards compare it
    by identity with the entry they rendered and read ``session_key`` / ``choices`` /
    ``multi_select`` from it; answering goes through ``resolve_gateway_clarify``."""
    with _lock:
        entry = _entries.get(clarify_id)
        return entry if entry is not None and not entry.event.is_set() else None


def get_pending_for_session(session_key: str, *, include_choice_prompts: bool = False) -> Optional[_ClarifyEntry]:
    """Oldest pending entry awaiting free text (open-ended, or after "Other");
    ``include_choice_prompts=True`` returns the oldest unresolved entry of any kind (user
    typed at an active choice prompt: resolve it rather than queue a follow-up turn)."""
    with _lock:
        for cid in _session_index.get(session_key) or []:
            entry = _entries.get(cid)
            if entry is not None and (include_choice_prompts or entry.awaiting_text):
                return entry
        return None


def _match_label(text: str, choices: list[str]) -> Optional[str]:
    """Stripped choice text matching ``text`` case-insensitively, ignoring the '(Recommended)'
    suffix the first choice carries by the time it reaches adapters; None if no match."""
    from tools.clarify_tool import strip_recommended
    wanted = strip_recommended(text).casefold()
    for choice in choices:
        if strip_recommended(str(choice)).casefold() == wanted:
            return str(choice).strip()
    return None


def _split_tokens(text: str) -> Optional[list[str]]:
    """Comma-separated tokens, or space-separated all-numeric tokens ("1 3"); else None."""
    if "," in text:
        return [t.strip() for t in text.split(",") if t.strip()]
    parts = text.split()
    return parts if len(parts) > 1 and all(p.isdigit() for p in parts) else None


def _is_int(text: str) -> bool:
    try:
        int(text)
        return True
    except ValueError:
        return False


def _selection_attempt_tokens(text: str, choices: Optional[list[str]] = None) -> Optional[list[str]]:
    """Tokens when ``text`` looks like a typed selection (bare int, comma list,
    all-numeric space list); None for free prose so the gateway can release the
    clarify. Comma-list labels may span up to the longest choice's word count."""
    stripped = str(text).strip()
    if not stripped:
        return None
    tokens = _split_tokens(stripped)
    if tokens is None:
        digits = stripped.removeprefix("-")
        return [stripped] if digits.isdigit() or _is_int(stripped) else None
    if "," not in stripped or not tokens:
        return tokens or None
    max_words = max(1, max((len(str(c).split()) for c in choices or []), default=1))
    return tokens if all(t.isdigit() or len(t.split()) <= max_words for t in tokens) else None


def _coerce_text_response_detailed(entry: _ClarifyEntry, response: str) -> tuple[Optional[str], Optional[str]]:
    """Map a typed reply to ``(value, None)`` or ``(None, reason)``: ``"invalid_selection"``
    (selection-shaped but out of range/unrecognised — keep the clarify armed for a retry) or
    ``"prose"`` (free text on a native choice prompt — the gateway may cancel and route normally
    so a redirect-to-steer path cannot deadlock behind the waiting tool). Open-ended entries and
    ``awaiting_text`` accept any text; numeric picks and exact labels always resolve; multi-select
    returns a JSON array string (decoded tool-side); one bad token rejects the whole reply."""
    text = str(response).strip()
    is_skip = text.casefold() == SKIP_WORD
    if not entry.choices:
        return (SKIPPED if is_skip else text), None
    if entry.multi_select:
        coerced = _coerce_multi_select_text(entry, text)
        selection_shaped = _selection_attempt_tokens(text, entry.choices) is not None
    else:
        # Out-of-range / non-canonical integer is a failed selection, not prose.
        selection_shaped = _is_int(text)
        idx = int(text) - 1 if selection_shaped else -1
        coerced = entry.choices[idx] if 0 <= idx < len(entry.choices) else _match_label(text, entry.choices)
    if coerced is not None:
        return coerced, None
    if is_skip:
        return SKIPPED, None
    if entry.awaiting_text:
        return text, None
    return None, "invalid_selection" if selection_shaped else "prose"


def _coerce_multi_select_text(entry: _ClarifyEntry, text: str) -> Optional[str]:
    """Parse "1,3" / "1 3" / "staging, prod" into a JSON array of choice labels;
    None when any token is out of range or unrecognised (reject the whole reply)."""
    choices, selected = entry.choices or [], []
    if not text:
        return None
    tokens = _split_tokens(text)
    for token in [text] if tokens is None else tokens:
        if token.isdigit():
            idx = int(token) - 1
            label = str(choices[idx]).strip() if 0 <= idx < len(choices) else None
        else:
            label = _match_label(token, choices)
        if label is None:
            return None
        if label not in selected:
            selected.append(label)
    return json.dumps(selected, ensure_ascii=False) if selected else None


def attempt_text_response(clarify_id: str, response: str) -> str:
    """Try to resolve the entry ``clarify_id`` names from typed text; returns a TEXT_* outcome. A
    reply checked against one question (its requester) never settles another raised meanwhile."""
    return _attempt_text_response(get_pending(clarify_id), response)


def _attempt_text_response(entry: Optional[_ClarifyEntry], response: str) -> str:
    if entry is None:
        return TEXT_NO_PENDING
    coerced, reason = _coerce_text_response_detailed(entry, response)
    if coerced is None:
        return TEXT_REJECTED_SELECTION if reason == "invalid_selection" else TEXT_REJECTED_PROSE
    if resolve_gateway_clarify(entry.clarify_id, coerced):
        return TEXT_RESOLVED
    return TEXT_NO_PENDING  # lost a race with a button/callback resolution — no work left


def mark_awaiting_text(clarify_id: str) -> bool:
    """Flip an entry into text-capture mode (user picked 'Other'); False if unknown."""
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is not None:
            entry.awaiting_text = True
        return entry is not None


def has_pending(session_key: str) -> bool:
    """True when this session has at least one pending clarify entry."""
    with _lock:
        return any(_entries.get(cid) is not None for cid in _session_index.get(session_key) or [])


def clear_session(session_key: str) -> int:
    """Drop every pending clarify for a session (``/new``, shutdown, cached-agent eviction) so
    blocked agent threads don't outlive it; returns how many were cancelled. First-writer-wins: an already-set entry was answered for
    real, so it is dropped but its response preserved. The loop stays inside the lock so a button
    callback cannot slip between pop and check; entries go regardless of state so a cleared
    session is never resurrected by late callbacks."""
    with _lock:
        cancelled = 0
        for entry in (_entries.pop(cid, None) for cid in list(_session_index.pop(session_key, []) or [])):
            if entry is None:
                continue
            # A cleared session never answers its polls: drop the poll record too, so a
            # later vote finds nothing to resolve (the poll itself is stopped by retire,
            # or by its own open_period if the process went away).
            poll = _polls.pop(entry.clarify_id, None)
            if poll is not None:
                _poll_index.pop(poll.poll_id, None)
            if entry.event.is_set():
                continue
            entry.response = CANCELLED
            entry.event.set()
            cancelled += 1
    return cancelled


# Clarify wait is FIXED at ten minutes on every surface (gateway, CLI, TUI):
# the poll's open_period, the agent's wait and the timeout notice all read
# this one value, and no config key changes it.
CLARIFY_TIMEOUT_SECONDS = 600


def resolve_clarify_timeout(config: dict) -> int:
    """Clarify timeout (seconds): fixed at :data:`CLARIFY_TIMEOUT_SECONDS` (10 minutes).

    The single source of truth for every surface (gateway, CLI, TUI). ``config``
    is accepted for call-site compatibility but no longer consulted — a clarify
    poll expires after ten minutes, always.
    """
    return CLARIFY_TIMEOUT_SECONDS


def get_clarify_timeout() -> int:
    """Clarify timeout from :func:`resolve_clarify_timeout`: fixed at ten minutes."""
    return CLARIFY_TIMEOUT_SECONDS
