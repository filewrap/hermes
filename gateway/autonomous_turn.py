"""Per-turn autonomous wiring for the gateway runner (``TurnRunner`` collaborator).

The runner calls these around a turn; the state itself lives in
``gateway/autonomous_state.py`` (run store + live flag) and the per-turn
flag in ``agent/autonomous_context.py`` (contextvar). This module owns the
seams between them and the turn:

- :func:`begin` — read the live flag + run record at turn start; evict the
  cached agent when the flag flipped since that agent was built
  (cached-agent trap: construction bakes in ``model_config`` state, and the
  config signature does not cover the autonomous flag, so a flip must bust
  the cache or the turn silently reuses the wrong agent).
- :func:`stamp_run_model` — stamp the turn's resolved model onto the run
  record, the same stamping the aborted-turn code does
  (``turn_model_from_agent``), so a model change starts fresh, never resumes
  under the wrong model.
- :func:`maybe_prepend_start_message` — the one run-start line, once per
  run, as ordinary conversation content (prepended to the user message, so
  it is persisted in the transcript and visible to the owner). Zero prompt
  bytes: no system-prompt, prompt-builder or toolset change anywhere here.
- :func:`maybe_prepend_stop_message` — the one run-closed line, once, on
  the next turn in the stopped run's session (armed by ``disable()``):
  ordinary conversation content, same shape as the run-start line, so the
  agent treats the old task as closed instead of continuing it from the
  transcript. Consumed on first use; an explicit Resume drops it unseen.
- :func:`arm_conversation` / :func:`disarm_conversation` — the per-turn
  bypass around ``run_conversation``: the contextvar, the
  ``tools/approval`` per-key session set, and the run-budget override.
  ``run_budget_seconds`` is checked and ignored while the flag is on, so an
  owner-set wall clock cannot kill a run; the previous value is restored
  afterwards, so a cached agent's budget survives the turn.
- :func:`install_tool_checkpoint` / :func:`checkpoint_tool` — checkpoint
  the run record after each completed tool call (hence after each completed
  tool round): steps, last tool, and the run's token totals. Best-effort —
  a failed checkpoint is logged, never raised into the turn.
- :func:`observe_watchdog` — feed the same completed tool call to the
  run's watchdog (``agent/autonomous_watchdog.py``): on a stuck episode it
  records the one-shot agent note, bumps the run's stall count, and hops
  the owner status + throttled pinned update onto the gateway loop. The
  watchdog observes only — it never blocks, halts, or caps the turn.

Per-turn state rides the ``TurnRunner`` instance (it is built per turn);
class-level defaults on ``TurnRunner`` keep the seams working on instances
built without ``__init__`` (``TurnRunner.__new__`` in tests). Nothing here
touches the prompt, taint refusals, admission, or runtime self-protection.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway import autonomous_state

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

#: The run-start line, verbatim. The goal (when the record names one) is
#: appended by :func:`maybe_prepend_start_message`. The clause names the
#: done channel: the run-start line is the only in-turn instruction the
#: agent gets about it (zero prompt bytes — no system-prompt, prompt-builder
#: or toolset change anywhere here), and the deferred bridge listing names
#: the tool on every later turn.
_AUTONOMOUS_START_TEXT = (
    "Autonomous mode is on: keep working until the goal is done. "
    "When the goal is done, or you need something only the owner can supply, "
    "call the autonomous_done tool."
)


def _turn_session_key(turn_runner: Any) -> Optional[str]:
    return getattr(getattr(turn_runner, "_ctx", None), "session_key", None)


def _is_active(turn_runner: Any) -> bool:
    return bool(getattr(turn_runner, "_autonomous_active", False))


def begin(turn_runner: Any) -> None:
    """Read the live flag for this turn and evict the cached agent on a flip.

    Resets the per-turn state first, so a turn that is not autonomous leaves
    nothing behind. When the flag is off the turn is ordinary — except the
    cached agent is still evicted if it was built under autonomous, so a
    flip in either direction rebuilds. Fail-closed throughout: an unreadable
    state file, a missing run record, or a run belonging to another session
    all run the turn as ordinary, never as autonomous.

    The flag is the owner's switch, not the run's: a failed or stopped run
    under a live flag keeps the mode on (the pin stays, the approval bypass
    arms in :func:`arm_conversation`) and only arms the run-closed note,
    so the agent does not continue the dead task from history. Only a done
    run (nothing resumable) or an idle one (never started — a crash between
    the end and the flag clear) clears the flag here.
    """
    turn_runner._autonomous_active = False
    turn_runner._autonomous_armed = False
    turn_runner._autonomous_mode_on = False
    turn_runner._autonomous_bypass_armed = False
    turn_runner._autonomous_home = None
    turn_runner._autonomous_run_id = None
    turn_runner._autonomous_baseline_in = None
    turn_runner._autonomous_baseline_out = None
    turn_runner._autonomous_last_in = None
    turn_runner._autonomous_last_out = None

    session_key = _turn_session_key(turn_runner)
    if not session_key:
        return
    try:
        # Looked up at call time, like autonomous_state.autonomous_mode_enabled,
        # so tests can patch the hermes_constants seam.
        from hermes_constants import get_routing_process_hermes_home

        home = get_routing_process_hermes_home()
        state = autonomous_state.load_state(home)
        run_id = state.get("current_run_id")
        active = bool(state.get("active")) and isinstance(run_id, str) and bool(run_id)
        record = autonomous_state.load_run(home, run_id) if active else None
        if active and record is None:
            logger.warning(
                "Autonomous flag is on for %s but run %s has no record; running ordinary",
                session_key, run_id,
            )
            active = False
        if active and record is not None:
            record_key = record.get("session_key")
            if record_key is not None and record_key != session_key:
                logger.info(
                    "Autonomous run %s belongs to another session; running ordinary",
                    run_id,
                )
                active = False
        if active and record is not None:
            # The run's own state decides the turn. A waiting run resumes
            # on the owner's next message — any turn in this DM session is
            # that message, or the run's own child completing into it.
            state_now = str(record.get("state") or "")
            if state_now == "needs_input":
                try:
                    autonomous_state.resume_run(home, run_id)
                except Exception:
                    logger.debug(
                        "Autonomous resume of waiting run %s failed; running ordinary",
                        run_id, exc_info=True,
                    )
                    active = False
            elif state_now in {"failed", "stopped"}:
                # A dead run never flips the mode off behind the owner: the
                # flag stays on (the pin stays, the bypass arms in
                # arm_conversation), and only the run-closed note arms, so
                # the next turn is told the task is closed instead of
                # continuing it from history. The turn runs without run
                # machinery — _autonomous_active stays False.
                try:
                    note = autonomous_state._closed_note_for(record)
                    if note is not None:
                        autonomous_state._arm_closed_note(home, note)
                except Exception:
                    logger.debug(
                        "Autonomous closed-note arm failed for run %s; running ordinary",
                        run_id, exc_info=True,
                    )
                logger.info(
                    "Autonomous run %s is %s under a live flag; mode stays on, "
                    "running without a live run",
                    run_id, state_now,
                )
                turn_runner._autonomous_mode_on = True
                active = False
            elif state_now in {"done", "idle"}:
                # A done run has nothing resumable and an idle one never
                # started — a crash between the end and the flag clear:
                # clear the stale flag, unpin what it left behind, and run
                # ordinary. Route through disable(): the one flag-clear path.
                try:
                    pinned = autonomous_state.disable(home)
                except Exception:
                    logger.debug(
                        "Autonomous stale-flag clear failed for run %s; running ordinary",
                        run_id, exc_info=True,
                    )
                    pinned = None
                logger.info(
                    "Autonomous run %s is %s under a live flag; flag cleared, running ordinary",
                    run_id, state_now,
                )
                active = False
                if pinned is not None:
                    _schedule_unpin(turn_runner, pinned)
        _evict_on_flip(turn_runner, session_key, active)
        if not active:
            return
        turn_runner._autonomous_active = True
        turn_runner._autonomous_mode_on = True
        turn_runner._autonomous_home = home
        turn_runner._autonomous_run_id = run_id
        _fold_in_toolset(turn_runner)
    except Exception:
        logger.debug("Autonomous turn begin failed; running ordinary", exc_info=True)
        turn_runner._autonomous_active = False
        turn_runner._autonomous_mode_on = False
        turn_runner._autonomous_home = None
        turn_runner._autonomous_run_id = None


#: The session-scoped toolset carrying the done channel. It is folded into
#: the turn's toolsets only while the turn is autonomous — never in
#: ``_HERMES_CORE_TOOLS``, so ordinary turns never see the tool at all
#: (no prompt bytes, no bridge listing entry, no load path). The fold-in
#: runs before the agent is built, so the agent-cache signature covers the
#: folded set, and the flip eviction rebuilds when the flag changes.
_AUTONOMOUS_TOOLSET = "autonomous"


def _fold_in_toolset(turn_runner: Any) -> None:
    """Add the autonomous toolset to this turn's toolsets, once.

    No-op when the turn context carries no toolset list (a bare test
    runner): the tool then stays invisible, fail-closed.
    """
    ctx = getattr(turn_runner, "_ctx", None)
    enabled = getattr(ctx, "enabled_toolsets", None) if ctx is not None else None
    if not isinstance(enabled, list) or _AUTONOMOUS_TOOLSET in enabled:
        return
    ctx.enabled_toolsets = [*enabled, _AUTONOMOUS_TOOLSET]


def _schedule_unpin(turn_runner: Any, pinned: Optional[dict[str, Any]]) -> None:
    """Hop an unpin + delete of a pinned message onto the gateway loop.

    Same shape as the watchdog sends: the turn runs on the agent's sync
    worker thread, so the adapter call hops via ``turn_runner._schedule``.
    Best-effort throughout — a missed unpin is logged, never raised.
    """
    if not pinned:
        return
    try:
        from plugins.platforms.telegram.autonomous_callback import unpin_and_delete_pinned

        runner = getattr(turn_runner, "_runner", None)
        ctx = getattr(turn_runner, "_ctx", None)
        source = getattr(ctx, "source", None) if ctx is not None else None
        schedule = getattr(turn_runner, "_schedule", None)
        if runner is None or source is None or not callable(schedule):
            return
        if getattr(ctx, "_loop_for_step", None) is None:
            return
        adapter = runner._delivery_adapter_for(source)
        if adapter is None:
            return
        _schedule_send(turn_runner, schedule,
                       unpin_and_delete_pinned(adapter, pinned),
                       "Autonomous pinned unpin")
    except Exception:
        logger.debug("Autonomous unpin schedule failed", exc_info=True)


def _evict_on_flip(turn_runner: Any, session_key: str, active: bool) -> None:
    """Evict the session's cached agent when it was built under the other flag.

    The stamp is ``_autonomous_at_build`` (set when the agent is built); an
    agent that predates the stamp reads as an ordinary build. No cached
    agent, or a stamp that already matches, is a no-op — the prompt cache
    survives every turn except a real flip.
    """
    runner = getattr(turn_runner, "_runner", None)
    cache_lock = getattr(runner, "_agent_cache_lock", None)
    cache = getattr(runner, "_agent_cache", None)
    if cache_lock is None or cache is None:
        return
    evicted = None
    built = False
    with cache_lock:
        entry = cache.get(session_key)
        cached = entry[0] if entry else None
        if cached is None:
            return
        built_flag = getattr(cached, "_autonomous_at_build", None)
        built = bool(built_flag) if built_flag is not None else False
        if built != active:
            evicted = turn_runner._pop_cached_agent_for_eviction()
    if evicted is not None:
        logger.info(
            "Autonomous flag flipped for %s; evicting the cached agent built under %s",
            session_key, "autonomous" if built else "ordinary",
        )
        turn_runner._release_evicted_agent(evicted)


def stamp_run_model(turn_runner: Any, agent: Any) -> None:
    """Stamp the turn's resolved model onto the run record.

    The same stamping the aborted-turn code does (``turn_model_from_agent``):
    the record carries ``{"model", "provider"}`` so a model change starts
    fresh, never resumes under the wrong model. Best-effort — a store
    failure never fails the turn.
    """
    if not _is_active(turn_runner):
        return
    home = getattr(turn_runner, "_autonomous_home", None)
    run_id = getattr(turn_runner, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    try:
        from gateway.session_lifecycle import turn_model_from_agent

        model = turn_model_from_agent(agent)
        if model is not None:
            autonomous_state.checkpoint(home, run_id, model=model)
    except Exception:
        logger.debug("Autonomous run model stamp failed for %s", run_id, exc_info=True)


def maybe_prepend_start_message(turn_runner: Any) -> None:
    """Prepend the one run-start line to the turn's message, once per run.

    Called at the end of ``_prepare_turn_message`` so the line lands first,
    ahead of the pending notes. Only the run's first turn gets it (tracked
    per run id on the runner; run ids are unique, so a stale entry can never
    suppress a later run). An empty turn message gets nothing — the line
    never invents a user message.
    """
    if not _is_active(turn_runner):
        return
    ctx = getattr(turn_runner, "_ctx", None)
    run_id = getattr(turn_runner, "_autonomous_run_id", None)
    message = getattr(ctx, "message", None)
    if ctx is None or run_id is None or not isinstance(message, str) or not message.strip():
        return
    runner = getattr(turn_runner, "_runner", None)
    if runner is None:
        return
    noted = getattr(runner, "_autonomous_start_noted", None)
    if noted is None:
        noted = set()
        try:
            runner._autonomous_start_noted = noted
        except Exception:
            logger.debug(
                "Autonomous start-note tracking unavailable; skipping the run-start line",
                exc_info=True,
            )
            return
    if run_id in noted:
        return
    noted.add(run_id)
    text = _AUTONOMOUS_START_TEXT
    home = getattr(turn_runner, "_autonomous_home", None)
    if home is not None:
        record = autonomous_state.load_run(home, run_id)
        goal = (record or {}).get("goal")
        if isinstance(goal, str) and goal.strip():
            text = f"{text} Goal: {goal.strip()}"
    ctx.message = f"{text}\n\n{message}"


#: The run-closed line, verbatim. The goal (when the note names one) is
#: appended by :func:`maybe_prepend_stop_message`. Same shape as the
#: run-start line: ordinary conversation content, never a prompt rebuild.
_AUTONOMOUS_STOP_TEXT = (
    "The previous autonomous run was stopped by the owner. "
    "Its task is closed — do not continue it. "
    "Treat the owner's new message as a fresh request."
)


def maybe_prepend_stop_message(turn_runner: Any) -> None:
    """Prepend the one run-closed line to the next turn's message, once.

    Consumes the closure note armed by ``disable()`` for the stopped run's
    session (see :func:`gateway.autonomous_state.take_closed_note`). The
    line lands as ordinary conversation content — persisted in the
    transcript and visible to the owner — so the agent reads the old task
    as closed instead of picking it up from history. No-op when no note
    is pending for this session, or when the turn's message is empty (the
    note stays pending for the next real message).
    """
    ctx = getattr(turn_runner, "_ctx", None)
    message = getattr(ctx, "message", None) if ctx is not None else None
    if not isinstance(message, str) or not message.strip():
        return
    session_key = _turn_session_key(turn_runner)
    if not session_key:
        return
    try:
        # Looked up at call time, like begin(), so tests can patch the
        # hermes_constants seam.
        from hermes_constants import get_routing_process_hermes_home

        home = get_routing_process_hermes_home()
        note = autonomous_state.take_closed_note(home, session_key)
    except Exception:
        logger.debug("Autonomous stop message lookup failed; skipping", exc_info=True)
        return
    if note is None:
        return
    text = _AUTONOMOUS_STOP_TEXT
    goal = note.get("goal") if isinstance(note, dict) else None
    if isinstance(goal, str) and goal.strip():
        text = f"{text} Goal: {goal.strip()}"
    ctx.message = f"{text}\n\n{message}"


def arm_conversation(turn_runner: Any, agent: Any, session_key: str) -> Optional[float]:
    """Arm the per-turn autonomous state around ``run_conversation``.

    Two decisions, split by what they guard. The approval bypass is
    MODE-level: it arms whenever the mode flag is on at turn start
    (``_autonomous_mode_on``, set by :func:`begin`), even when no live
    run backs the turn — a failed or stopped run never turns the mode off
    behind the owner, so the turn still takes no approvals. The run
    machinery — token baselines, the run-budget check, the nulled
    ``run_budget_seconds`` — is RUN-level: it arms only for a live run.
    A mode-on turn with a dead run is a guard-free chat turn, not a run
    turn: the bypass arms, the budget stays the owner's. Returns the
    agent's previous budget (``None`` when the turn is not autonomous, or
    when only the bypass armed and no budget was nulled).
    """
    if not getattr(turn_runner, "_autonomous_mode_on", False):
        return None
    from agent.autonomous_context import set_autonomous_turn
    from tools.approval import enable_session_autonomous

    set_autonomous_turn(True)
    if session_key:
        enable_session_autonomous(session_key)
    turn_runner._autonomous_bypass_armed = True
    if not _is_active(turn_runner):
        return None
    baseline_in = int(getattr(agent, "session_prompt_tokens", 0) or 0)
    baseline_out = int(getattr(agent, "session_completion_tokens", 0) or 0)
    turn_runner._autonomous_baseline_in = baseline_in
    turn_runner._autonomous_baseline_out = baseline_out
    turn_runner._autonomous_last_in = baseline_in
    turn_runner._autonomous_last_out = baseline_out
    # Autonomous backbone (§3): stamp the run's budget context on the
    # coordinator so its turn breaks when the run cap is spent and its
    # workers inherit the run id. The coordinator has no _on_response_usage
    # hook — its tokens arrive only through checkpoint_tool, never twice.
    agent._autonomous_run_home = turn_runner._autonomous_home
    agent._autonomous_run_id = turn_runner._autonomous_run_id
    agent._autonomous_run_budget_check = autonomous_state.make_run_budget_check(
        turn_runner._autonomous_home, turn_runner._autonomous_run_id)
    prev_budget = getattr(agent, "run_budget_seconds", None)
    agent.run_budget_seconds = None
    turn_runner._autonomous_armed = True
    return prev_budget


def finish_done_run(turn_runner: Any, agent: Any) -> None:
    """End-of-turn cleanup for a run the agent declared done.

    Called from :func:`disarm_conversation` (the turn's ``finally``), so it
    runs however the turn ends. The run record is the durable truth: when
    it reads ``done``, the live flag clears and the pinned message is
    unpinned + deleted — ``disable()`` tolerates an already-terminal
    record, so the record stays ``done``. A waiting run (``needs_input``)
    is left alone: the flag and the pin stay, and the owner's next message
    resumes it via :func:`begin`.

    End-states, as decided for the done channel:

    - done → record done, unpin, live flag cleared. The MASTER setting
      stays on: the mode is still enabled, and the owner starts the next
      goal with ``/autonomous enable`` — a done run is not resumable, so
      re-enable goes straight to a fresh run with no ask.
    - needs_input → record waiting, flag and pin stay; resume on the next
      message in this session.

    Consequence to know: the flag clears in the turn's ``finally``, before
    the gateway delivers the final response, so the completion notice
    delivers WITHOUT the green tag (the tag gate reads the live flag at
    delivery) — consistent with the disable confirmation. Best-effort
    throughout: a failed cleanup is logged, never raised into the turn.
    """
    if not _is_active(turn_runner):
        return
    home = getattr(turn_runner, "_autonomous_home", None)
    run_id = getattr(turn_runner, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    try:
        from agent.autonomous_context import consume_autonomous_end

        # Drain any still-armed end request so it cannot leak into a later
        # turn on the cached agent (the turn round already consumed the
        # normal-path one; an interrupted turn never reached it).
        consume_autonomous_end(agent)
        record = autonomous_state.load_run(home, run_id)
        if record is None or str(record.get("state") or "") != "done":
            return
        pinned = autonomous_state.disable(home, stopped_by="autonomous_done")
    except Exception:
        logger.debug("Autonomous done cleanup failed for run %s", run_id, exc_info=True)
        return
    if pinned is not None:
        _schedule_unpin(turn_runner, pinned)


def finish_budget_failed_run(turn_runner: Any, agent: Any) -> None:
    """End-of-turn cleanup for a run that spent its token budget (§3).

    Called from :func:`disarm_conversation` (the turn's ``finally``), after
    :func:`finish_done_run`. When the turn broke on the RUN budget
    (``agent._autonomous_budget_exceeded == "run"``), the run is marked
    failed with reason ``"budget:..."`` and the live flag clears — but NOT
    via ``disable()``: that would flip failed → stopped, an illegal
    transition. A budget-failed run stays resumable by design (only KILLED
    runs are non-resumable, §4). A worker-only break (``"worker"``) leaves
    the run alone: the worker stopped, the coordinator keeps going.
    Best-effort throughout: a failed cleanup is logged, never raised.
    """
    if not _is_active(turn_runner):
        return
    if getattr(agent, "_autonomous_budget_exceeded", None) != "run":
        return
    home = getattr(turn_runner, "_autonomous_home", None)
    run_id = getattr(turn_runner, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    try:
        record = autonomous_state.load_run(home, run_id)
        if record is None:
            return
        total = autonomous_state.run_token_total(record)
        cap = autonomous_state._config_max_tokens_per_run(home)
        autonomous_state.mark_failed(
            home, run_id, last_error=f"budget: run used {total:,} of {cap:,} tokens")
        autonomous_state.set_active(home, False)
        pinned = autonomous_state.clear_pinned_message(home)
    except Exception:
        logger.debug("Autonomous budget-failed cleanup failed for run %s", run_id, exc_info=True)
        return
    if pinned is not None:
        _schedule_unpin(turn_runner, pinned)


def disarm_conversation(turn_runner: Any, agent: Any, session_key: str,
                        prev_budget: Optional[float]) -> None:
    """Undo :func:`arm_conversation`: restore the budget, drop the bypass.

    The bypass drops whenever its latch is set — including a turn that
    armed only the bypass (mode on, no live run), where there is no budget
    to restore and no end-of-run cleanup to run. The budget restore and
    the done / budget-failed cleanup stay gated on the run latch, in the
    same order as before. No-op when neither latch is set, so a turn that
    never armed — or an arm that failed midway — never touches the agent
    or the session set. Always runs from the turn's ``finally``.
    """
    bypass_armed = bool(getattr(turn_runner, "_autonomous_bypass_armed", False))
    run_armed = bool(getattr(turn_runner, "_autonomous_armed", False))
    if not bypass_armed and not run_armed:
        return
    turn_runner._autonomous_bypass_armed = False
    turn_runner._autonomous_armed = False
    if run_armed:
        try:
            agent.run_budget_seconds = prev_budget
        except Exception:
            logger.debug("Autonomous budget restore failed", exc_info=True)
    if bypass_armed:
        from agent.autonomous_context import set_autonomous_turn
        from tools.approval import disable_session_autonomous

        if session_key:
            disable_session_autonomous(session_key)
        set_autonomous_turn(False)
    if run_armed:
        finish_done_run(turn_runner, agent)
        finish_budget_failed_run(turn_runner, agent)


def install_tool_checkpoint(turn_runner: Any, agent: Any) -> None:
    """Wrap the agent's tool-complete callback with a run checkpoint.

    Installed after ``_wire_turn_agent_callbacks`` so it wraps whatever that
    wiring settled on (including None). The wrapper checkpoints first, then
    feeds the same call to the watchdog, then forwards to the previous
    callback with identical arguments, so native cards and progress
    consumers see exactly what they saw before.
    """
    if not _is_active(turn_runner):
        return
    previous = getattr(agent, "tool_complete_callback", None)

    def _checkpoint_then_forward(call_id, tool_name, args, result, _prev=previous):
        checkpoint_tool(turn_runner, tool_name)
        observe_watchdog(turn_runner, tool_name, args, result)
        if _prev is not None:
            _prev(call_id, tool_name, args, result)

    agent.tool_complete_callback = _checkpoint_then_forward


def checkpoint_tool(turn_runner: Any, tool_name: Any) -> None:
    """Checkpoint the run ledger after one completed tool call.

    Steps count completed tool calls; the token fields carry the run's
    totals — each checkpoint adds only the tokens since the previous
    checkpoint to the stored ledger. Never raises into the turn.
    """
    if not _is_active(turn_runner):
        return
    home = getattr(turn_runner, "_autonomous_home", None)
    run_id = getattr(turn_runner, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    try:
        record = autonomous_state.load_run(home, run_id)
        if record is None:
            return
        updates: dict[str, Any] = {
            "steps": int(record.get("steps") or 0) + 1,
            "last_tool": str(tool_name or "tool"),
        }
        holder = getattr(getattr(turn_runner, "_ctx", None), "agent_holder", None)
        agent = holder[0] if isinstance(holder, list) and holder else None
        last_in = getattr(turn_runner, "_autonomous_last_in", None)
        last_out = getattr(turn_runner, "_autonomous_last_out", None)
        if agent is not None and last_in is not None and last_out is not None:
            now_in = int(getattr(agent, "session_prompt_tokens", 0) or 0)
            now_out = int(getattr(agent, "session_completion_tokens", 0) or 0)
            updates["input_tokens"] = int(record.get("input_tokens") or 0) + max(0, now_in - last_in)
            updates["output_tokens"] = int(record.get("output_tokens") or 0) + max(0, now_out - last_out)
            turn_runner._autonomous_last_in = now_in
            turn_runner._autonomous_last_out = now_out
        autonomous_state.checkpoint(home, run_id, **updates)
    except Exception:
        logger.debug("Autonomous checkpoint failed for run %s", run_id, exc_info=True)


#: Cap on run watchdogs kept on the runner (one per run id; disabled runs
#: are never re-armed, so the oldest entry is dropped past the cap).
_WATCHDOG_STORE_CAP = 32


def _watchdog_for(turn_runner: Any, run_id: str) -> Any:
    """The run's watchdog, shared across turns via the runner (same shape
    as ``_autonomous_start_noted``); a bare turn runner keeps its own."""
    from agent.autonomous_watchdog import AutonomousWatchdog

    runner = getattr(turn_runner, "_runner", None)
    store = None
    if runner is not None:
        store = getattr(runner, "_autonomous_watchdogs", None)
        if not isinstance(store, dict):
            store = {}
            try:
                runner._autonomous_watchdogs = store
            except AttributeError:
                store = None
    if store is None:
        watchdog = getattr(turn_runner, "_autonomous_watchdog", None)
        if watchdog is None:
            watchdog = AutonomousWatchdog()
            try:
                turn_runner._autonomous_watchdog = watchdog
            except Exception:
                logger.debug("Autonomous watchdog store unavailable", exc_info=True)
        return watchdog
    watchdog = store.get(run_id)
    if watchdog is None:
        watchdog = AutonomousWatchdog()
        if len(store) >= _WATCHDOG_STORE_CAP:
            store.pop(next(iter(store)), None)
        store[run_id] = watchdog
    return watchdog


def record_watchdog_note(runner: Any, session_key: str, note: str) -> None:
    """Store the watchdog's one-shot agent note on the runner's per-session
    dict, same shape as ``_pending_delivery_failure_notes`` (popped once by
    ``_prepend_pending_note`` in ``_prepare_turn_message``). Never raises:
    feedback must not break the turn it follows."""
    if runner is None or not session_key or not note:
        return
    notes = getattr(runner, "_pending_autonomous_watchdog_notes", None)
    if not isinstance(notes, dict):
        notes = {}
        try:
            runner._pending_autonomous_watchdog_notes = notes
        except Exception:
            logger.debug("Could not record autonomous watchdog note", exc_info=True)
            return
    notes[session_key] = note


def observe_watchdog(turn_runner: Any, tool_name: Any, args: Any, result: Any) -> None:
    """Feed one completed tool call to the run's watchdog.

    On a stuck episode, exactly three actions, in order: the one-shot
    agent note, the run's stall count, and the owner status + throttled
    pinned update (hopped onto the gateway loop — this hook runs on the
    agent's sync worker thread). Never raises into the turn, and never
    halts, blocks, or caps anything: the run keeps going.
    """
    if not _is_active(turn_runner):
        return
    home = getattr(turn_runner, "_autonomous_home", None)
    run_id = getattr(turn_runner, "_autonomous_run_id", None)
    if home is None or run_id is None:
        return
    try:
        from agent.autonomous_watchdog import build_agent_note

        record = autonomous_state.load_run(home, run_id)
        if record is None:
            return
        watchdog = _watchdog_for(turn_runner, run_id)
        episode = watchdog.observe(
            tool_name, args, result,
            steps=int(record.get("steps") or 0),
            input_tokens=int(record.get("input_tokens") or 0),
            output_tokens=int(record.get("output_tokens") or 0))
        if episode is None:
            return
        session_key = _turn_session_key(turn_runner)
        runner = getattr(turn_runner, "_runner", None)
        if session_key and runner is not None:
            record_watchdog_note(runner, session_key, build_agent_note(episode))
        try:
            autonomous_state.checkpoint(
                home, run_id, stall_count=int(record.get("stall_count") or 0) + 1)
        except Exception:
            logger.debug("Autonomous stall count checkpoint failed for %s", run_id, exc_info=True)
        _schedule_watchdog_sends(turn_runner, record, episode, watchdog)
    except Exception:
        logger.debug("Autonomous watchdog observe failed for run %s", run_id, exc_info=True)


def _schedule_send(turn_runner: Any, schedule: Any, coro: Any, log_message: str) -> None:
    """Hop one watchdog send onto the gateway loop; an un-schedulable
    coroutine is closed, never left dangling."""
    try:
        schedule(coro, log_message)
    except Exception:
        try:
            coro.close()
        except Exception:
            logger.debug("Autonomous watchdog coroutine close failed", exc_info=True)
        logger.debug("Autonomous watchdog send schedule failed: %s", log_message, exc_info=True)


def _schedule_watchdog_sends(turn_runner: Any, record: dict, episode: Any,
                             watchdog: Any) -> None:
    """Hop the owner status + pinned update onto the gateway loop.

    Best-effort throughout: the agent note is already recorded, so a
    missed send is logged, never raised. The owner notice is plain-text
    MTProto via the adapter's direct send (never ``_send_with_retry``,
    never the failed content); the pin edit is throttled by the watchdog
    (at most every 10 steps or 5 minutes) and skipped when nothing is
    pinned. No loop on the turn context (a bare test runner) means no
    sends — the note and the ledger still stand.
    """
    try:
        from agent.autonomous_watchdog import build_owner_notice, build_pin_text

        runner = getattr(turn_runner, "_runner", None)
        ctx = getattr(turn_runner, "_ctx", None)
        source = getattr(ctx, "source", None) if ctx is not None else None
        schedule = getattr(turn_runner, "_schedule", None)
        if runner is None or source is None or not callable(schedule):
            return
        if getattr(ctx, "_loop_for_step", None) is None:
            return
        adapter = runner._delivery_adapter_for(source)
        chat_id = getattr(source, "chat_id", None)
        if adapter is None or not chat_id:
            return
        goal = record.get("goal") if isinstance(record.get("goal"), str) else ""
        notice = build_owner_notice(goal=goal, episode=episode)
        _schedule_send(turn_runner, schedule,
                       adapter.send(chat_id=chat_id, content=notice),
                       "Autonomous stuck notice")
        home = getattr(turn_runner, "_autonomous_home", None)
        if home is None:
            return
        state = autonomous_state.load_state(home)
        pinned = state.get("pinned_message_id")
        if pinned is None or not watchdog.pin_update_allowed(episode.steps):
            return
        pin_chat = state.get("chat_id") or chat_id
        pin_text = build_pin_text(goal=goal, episode=episode,
                                  last_tool=record.get("last_tool"))
        _schedule_send(turn_runner, schedule,
                       adapter.edit_message(str(pin_chat), str(pinned), pin_text),
                       "Autonomous pinned update")
        watchdog.note_pin_updated(episode.steps)
    except Exception:
        logger.debug("Autonomous watchdog sends failed", exc_info=True)
