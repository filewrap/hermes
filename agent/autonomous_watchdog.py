"""Autonomous-mode watchdog: observe-only stuck detection.

The watchdog never blocks, halts, or caps a turn — it is not a guardrail
(the global guardrails are inert; see the build plan §1 item 1). It
observes the same tool-complete stream the run checkpoint uses
(``gateway/autonomous_turn.py`` feeds it from the checkpoint wrapper)
and, per run, tracks:

- identical tool + identical args streak;
- identical-error streak (the same failure, whatever the args);
- repeating cycles (the same sequence of tool calls, period >= 2);
- no-progress calls (consecutive tool results whose content hash repeats,
  i.e. the agent learned nothing new).

Thresholds (borrowed: Magentic-One ``max_stalls=3``): 3 consecutive
stalls of any one kind is "stuck". On stuck the watchdog reports an
:class:`StuckEpisode` — exactly once per episode — and the gateway glue
(``gateway/autonomous_turn.py``) does the three stuck actions in order:
a one-shot note to the agent, a short DM status to the owner, and a
throttled pinned-message update. There is no fourth action and no
backstop by design: a looping model burns tokens until the context
compacts or the owner hits Stop.

This module is pure: no gateway imports, no Telethon, no I/O. The
gateway glue owns the run record, the note dict, and the async sends.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Consecutive stalls of one kind that mean "stuck" (Magentic-One max_stalls).
_STUCK_THRESHOLD = 3
#: Longest cycle period the detector looks for (a period-1 repeat is the
#: identical-args streak, not a cycle).
_MAX_CYCLE_PERIOD = 6
#: Tool-call history kept for cycle detection (3 repeats of the longest period).
_HISTORY_LEN = 3 * _MAX_CYCLE_PERIOD
#: Pinned-message edit throttle: at most every 10 steps or 5 minutes.
_PIN_MIN_STEPS = 10
_PIN_MIN_SECONDS = 300.0
#: Bytes of a tool result hashed for the no-progress check, plus its length.
_RESULT_HASH_BYTES = 65536
_ERROR_TEXT_MAX = 200

_STALL_KINDS = ("identical_args", "identical_error", "cycle", "no_progress")


@dataclass
class StuckEpisode:
    """One stuck episode: what the run is stuck on, and its ledger snapshot."""

    kind: str
    tool: str
    count: int
    detail: str
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)


def _stable_args_key(args: Any) -> str:
    """A stable identity for a tool call's arguments (redacted display args
    are already a dict; anything else falls back to its string form)."""
    try:
        return json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _error_key(result: Any) -> Optional[str]:
    """The normalized error text when *result* is a failed tool result, else None.

    Tool errors arrive as a JSON ``{"error": ...}`` body (``tools/registry.py``
    ``tool_error``) or as a plain ``Error: ...`` string.
    """
    text = _result_text(result)
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error"):
            return str(parsed["error"]).strip()[:_ERROR_TEXT_MAX]
    lowered = stripped.lower()
    if lowered.startswith("error:") or lowered.startswith("error "):
        return stripped[:_ERROR_TEXT_MAX]
    return None


def _result_hash(result: Any) -> str:
    """Content hash of a tool result (first bytes + length, so a multi-MB
    read does not cost a full hash per call)."""
    text = _result_text(result)
    head = text[:_RESULT_HASH_BYTES].encode("utf-8", "ignore")
    return hashlib.sha256(head + str(len(text)).encode("ascii")).hexdigest()


class AutonomousWatchdog:
    """Per-run stuck detector. One instance per run record.

    ``observe`` is called once per completed tool call, in order. It
    returns a :class:`StuckEpisode` the first time a stall pattern reaches
    the threshold, then nothing until a progressing call ends the episode
    — the agent note and the owner status are therefore one-shot per
    episode, never a per-call spam.
    """

    def __init__(self, *, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.monotonic
        self._history: deque[tuple[str, str]] = deque(maxlen=_HISTORY_LEN)
        self._last_call: Optional[tuple[str, str]] = None
        self._identical_streak = 0
        self._last_error: Optional[str] = None
        self._error_streak = 0
        self._last_hash: Optional[str] = None
        self._hash_streak = 0
        self._episode: Optional[StuckEpisode] = None
        self._last_pin_steps: Optional[int] = None
        self._last_pin_at: Optional[float] = None

    @property
    def episode_active(self) -> bool:
        """True while a fired episode is still in progress."""
        return self._episode is not None

    def observe(self, tool_name: Any, args: Any, result: Any, *,
                steps: int = 0, input_tokens: int = 0,
                output_tokens: int = 0) -> Optional[StuckEpisode]:
        """Observe one completed tool call; the fired episode, or None.

        Never raises: a detector failure must not break the turn it observes.
        """
        try:
            return self._observe(tool_name, args, result,
                                 steps=steps, input_tokens=input_tokens,
                                 output_tokens=output_tokens)
        except Exception:
            logger.debug("Autonomous watchdog observe failed", exc_info=True)
            return None

    def _observe(self, tool_name: Any, args: Any, result: Any, *,
                 steps: int, input_tokens: int,
                 output_tokens: int) -> Optional[StuckEpisode]:
        tool = str(tool_name or "tool")
        call = (tool, _stable_args_key(args))
        error = _error_key(result)
        result_hash = _result_hash(result)

        # Streaks: each counts consecutive repeats of its own pattern.
        self._identical_streak = (
            self._identical_streak + 1 if call == self._last_call else 1)
        self._last_call = call
        self._history.append(call)
        self._error_streak = (
            self._error_streak + 1
            if error is not None and error == self._last_error else 1)
        self._last_error = error
        self._hash_streak = (
            self._hash_streak + 1 if result_hash == self._last_hash else 1)
        self._last_hash = result_hash

        episode = self._detect(tool, steps, input_tokens, output_tokens)
        if episode is not None:
            self._episode = episode
            return episode
        if self._episode is not None and self._is_progress(call, error, result_hash):
            # A call that breaks every stall pattern ends the episode: the
            # next stall is a new episode and may fire again.
            self._episode = None
            self._reset_streaks()
        return None

    def _detect(self, tool: str, steps: int, input_tokens: int,
                output_tokens: int) -> Optional[StuckEpisode]:
        """The first pattern at threshold wins; an active episode never re-fires."""
        if self._episode is not None:
            return None
        if self._identical_streak >= _STUCK_THRESHOLD:
            return StuckEpisode(
                kind="identical_args", tool=tool, count=self._identical_streak,
                detail=(f"called {tool} with identical arguments "
                        f"{self._identical_streak} times"),
                steps=steps, input_tokens=input_tokens, output_tokens=output_tokens)
        if self._error_streak >= _STUCK_THRESHOLD and self._last_error:
            return StuckEpisode(
                kind="identical_error", tool=tool, count=self._error_streak,
                detail=(f"called {tool} {self._error_streak} times and every call "
                        f"failed the same way ({self._last_error})"),
                steps=steps, input_tokens=input_tokens, output_tokens=output_tokens)
        cycle = self._cycle_repeat()
        if cycle is not None:
            period, tools = cycle
            return StuckEpisode(
                kind="cycle", tool=tool, count=_STUCK_THRESHOLD,
                detail=(f"repeated the same {period}-call sequence "
                        f"{_STUCK_THRESHOLD} times ({', '.join(tools)})"),
                steps=steps, input_tokens=input_tokens, output_tokens=output_tokens)
        if self._hash_streak >= _STUCK_THRESHOLD:
            return StuckEpisode(
                kind="no_progress", tool=tool, count=self._hash_streak,
                detail=(f"the last {self._hash_streak} tool calls returned "
                        "identical results"),
                steps=steps, input_tokens=input_tokens, output_tokens=output_tokens)
        return None

    def _cycle_repeat(self) -> Optional[tuple[int, list[str]]]:
        """(period, tools) when the tail of the history is one sequence
        repeated 3 times; period 1 is the identical-args streak, not a cycle."""
        history = list(self._history)
        for period in range(2, _MAX_CYCLE_PERIOD + 1):
            need = 3 * period
            if len(history) < need:
                continue
            tail = history[-need:]
            pattern = tail[:period]
            if tail == pattern * 3:
                return period, [name for name, _args in pattern]
        return None

    def _is_progress(self, call: tuple[str, str], error: Optional[str],
                     result_hash: str) -> bool:
        """A call that breaks every stall pattern at once: a new call, no
        repeated error, and a result unlike the last one."""
        return (
            self._identical_streak == 1
            and (error is None or self._error_streak == 1)
            and self._hash_streak == 1
        )

    def _reset_streaks(self) -> None:
        self._identical_streak = 1
        self._error_streak = 1 if self._last_error is not None else 0
        self._hash_streak = 1

    # --- Pinned-message throttle --------------------------------------------

    def pin_update_allowed(self, steps: int) -> bool:
        """True when the pinned message may be edited again: at least 10
        steps or 5 minutes since the last edit (whichever comes first)."""
        if self._last_pin_steps is None:
            return True
        if int(steps) - int(self._last_pin_steps) >= _PIN_MIN_STEPS:
            return True
        if self._last_pin_at is not None:
            return (self._clock() - self._last_pin_at) >= _PIN_MIN_SECONDS
        return True

    def note_pin_updated(self, steps: int) -> None:
        """Record a pinned-message edit (called only after a successful edit)."""
        self._last_pin_steps = int(steps)
        self._last_pin_at = self._clock()


def build_agent_note(episode: StuckEpisode) -> str:
    """The one-shot note the agent sees on its next turn: what it repeated,
    that the last results were identical, and the three options it owns.
    The agent decides — the note never orders a halt."""
    return (
        "[Autonomous watchdog note: you have "
        f"{episode.detail}; the last results were identical. "
        "Change strategy, use what you have, or declare done.]"
    )


def build_owner_notice(*, goal: str, episode: StuckEpisode) -> str:
    """The short DM status to the owner: goal, ledger, what it is stuck on,
    and that it keeps running unless the owner says /stop. Informational —
    never a question that blocks the run."""
    from agent.i18n import t

    return t(
        "gateway.autonomous.stuck_notice",
        goal=(goal or "untitled run").strip() or "untitled run",
        detail=episode.detail, steps=episode.steps,
        tokens=episode.total_tokens)


def build_pin_text(*, goal: str, episode: StuckEpisode,
                   last_tool: Optional[str] = None) -> str:
    """The pinned status message: steps / tokens / last activity / stuck detail."""
    from agent.i18n import t

    return t(
        "gateway.autonomous.stuck_pin",
        goal=(goal or "untitled run").strip() or "untitled run",
        steps=episode.steps, tokens=episode.total_tokens,
        tool=last_tool or episode.tool, detail=episode.detail)
