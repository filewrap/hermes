"""Acknowledgement fast path (beta): bare "thanks" / "ok" / "👍" messages
answered with a reaction and no model call.

When ``platforms.telegram.ack_fast_path`` is on for the active profile, an
inbound text message that is ONLY an acknowledgement gets a 👍 reaction on
the user's message and no turn starts. Everything else — questions, tasks,
answers to pending clarifies or approvals, commands, media — runs the full
turn unchanged. Any doubt goes to the full turn: the matcher is a small
explicit set plus a bare-emoji rule, never a heuristic guess.

The matcher is pure (no I/O, no gateway state) so it is unit-testable in
isolation. Pending-state checks live in the caller
(``gateway/run_inbound.py::_hm_try_ack_fast_path``), which also owns the
reaction delivery and fails open to the full turn on any error.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_CONFIG_KEY = "ack_fast_path"
_RUNTIME_ATTR = "_ack_fast_path_runtime"
REACTION = "👍"

# Reaction delivery timeout, mirroring the intent-reaction precedent
# (plugins/platforms/telegram/incoming_reactions.py) — a reaction that
# hangs must never stall the gateway loop. Value only, never imported
# from the plugin: core must not import plugin code (boundary lint).
REACTION_TIMEOUT_SECONDS = 2.0

# Bare acknowledgements only. Deliberately excluded: yes / yeah / haan
# (answers, not acknowledgements) and great / perfect / nice / good / cool /
# acha (ambiguous — they can answer "is it good?").
_ACK_PHRASES = frozenset({
    "thanks",
    "thank you",
    "thx",
    "ty",
    "shukriya",
    "ok",
    "okay",
    "k",
    "chalo",
    "theek hai",
    "theek",
    "got it",
    "noted",
})

_MAX_ACK_CHARS = 24
_TRAILING_PUNCT = ".,!"
_URL_RE = re.compile(r"(?i)(?:https?://|www\.|t\.me/)")
_VARIATION_SELECTOR = "\ufe0f"
_ZERO_WIDTH_JOINER = "\u200d"


def ack_fast_path_enabled(adapter: Any) -> bool:
    """Profile-wide ``platforms.telegram.ack_fast_path``; False when unset.

    The live runtime attr (set by the /settings toggle) wins over the
    persisted config value, exactly like the threaded-replies reader.
    """
    runtime = getattr(adapter, _RUNTIME_ATTR, None)
    if runtime is not None:
        return bool(runtime)
    extra = getattr(getattr(adapter, "config", None), "extra", None)
    if isinstance(extra, dict):
        return bool(extra.get(_CONFIG_KEY, False))
    return False


def _normalize(text: str) -> str:
    """Lowercased, whitespace-collapsed acknowledgement candidate."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip().casefold())
    return collapsed.rstrip(_TRAILING_PUNCT).strip()


def _is_bare_emoji(text: str) -> bool:
    """True when *text* is 1–3 emoji and nothing else.

    Emoji are Unicode symbols (category So/Sk) plus variation selectors and
    zero-width joiners (compound emoji). Any letter, digit, whitespace, or
    ASCII punctuation fails the match — the text must be emoji only.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 6:
        return False
    count = 0
    for char in stripped:
        if char in (_VARIATION_SELECTOR, _ZERO_WIDTH_JOINER):
            continue
        if unicodedata.category(char).startswith(("S",)):
            count += 1
            continue
        return False
    return 1 <= count <= 3


def is_acknowledgement_only(text: Any) -> bool:
    """True when *text* is only an acknowledgement: one phrase from the
    explicit set, or 1–3 bare emoji. Anything else — questions, tasks,
    commands, links, longer text — is False (full turn)."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_ACK_CHARS:
        return False
    if stripped.startswith("/"):
        return False
    if "?" in stripped:
        return False
    if _URL_RE.search(stripped):
        return False
    if _is_bare_emoji(stripped):
        return True
    return _normalize(stripped) in _ACK_PHRASES


def should_fast_path(
    *,
    is_ack: bool,
    setting_on: bool,
    has_reaction: bool,
    has_pending_approval: bool,
    has_answerable_pending_clarify: bool,
    reply_answers_question: bool,
) -> bool:
    """The fast-path decision, as a pure function of its inputs.

    Every guard must pass: the text is a bare acknowledgement, the beta is
    on, the adapter can react, and nothing is waiting — no pending approval,
    no answerable pending clarify, and the message is not an answer to a
    question the agent asked. Any doubt returns False (full turn).
    """
    return bool(
        is_ack
        and setting_on
        and has_reaction
        and not has_pending_approval
        and not has_answerable_pending_clarify
        and not reply_answers_question
    )
