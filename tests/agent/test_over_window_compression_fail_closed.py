"""An over-window session that compression cannot shrink must not block the host toward the
compression ceiling or send a request the model cannot accept (#116472).

Two invariants: turn-start preflight ends the turn at once with ``/new`` guidance when a pass made
no progress on a request still above the model window (a fitting or unknown-window request keeps
the send-as-is behaviour); and the pre-commit wait for an over-window request is bounded by ONE
inactivity budget (``compression.context_timeout_seconds``) instead of the generic ceiling, so a
summary that keeps streaming while reclaiming nothing cannot hold the session for minutes.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_compression import run_compress_context_with_progress_timeout
from agent.turn_context import PreflightCompressionTimedOut
from agent.turn_context_compaction import CompactionOutcome, _run_preflight_passes


def _agent(*, context_length):
    compressor = SimpleNamespace(
        protect_first_n=3, protect_last_n=3, threshold_tokens=65_536, context_length=context_length,
        summary_target_ratio=0.5, should_compress=lambda tokens: tokens >= 65_536,
    )
    agent = SimpleNamespace(
        context_compressor=compressor, session_id="s1", model="m", max_compression_attempts=3,
        _emit_status=MagicMock(), _compression_blocked_transient=None,
    )
    agent._compress_context = lambda msgs, system, **kw: (msgs, system)  # a pass that reclaims nothing
    return agent


def _preflight(agent, request_tokens):
    out = CompactionOutcome(
        messages=[{"role": "user", "content": "hi"}], active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=0,
    )
    with patch("agent.turn_context._preflight_request_tokens", return_value=request_tokens), patch(
        "agent.turn_context_compaction.automatic_compaction_status_message", return_value=""
    ):
        _run_preflight_passes(agent, out, agent.context_compressor, request_tokens, "sys", "t")
    return out


def test_no_progress_preflight_fails_closed_only_when_the_request_exceeds_the_window():
    with pytest.raises(PreflightCompressionTimedOut, match="Start a new session with /new"):
        _preflight(_agent(context_length=131_072), 356_113)

    # Over threshold but inside the window: sent as-is (blocked so the loop does not retry).
    assert _preflight(_agent(context_length=1_000_000), 356_113).blocked is True
    # Window unresolvable (0): never guess — the previous behaviour stands.
    assert _preflight(_agent(context_length=0), 356_113).blocked is True
    # The pass no-op'd on a transient guard (summary-failure cooldown): a defer, not proof.
    cooling = _agent(context_length=131_072)
    cooling._compression_blocked_transient = "cooldown:42"
    assert _preflight(cooling, 356_113).blocked is True


def _lock_skipping_agent(*, context_length):
    """A compressor double whose pass no-ops because another path holds
    the session's compression lock (the #69870 signal)."""
    agent = _agent(context_length=context_length)

    def _compress(msgs, system, **kw):
        agent._compression_skipped_due_to_lock = "pid=4242:tid=1:agent=deadbeef:nonce=abcd1234"
        return msgs, system

    agent._compress_context = _compress
    return agent


def test_lock_skip_preflight_fails_closed_only_when_the_request_exceeds_the_window():
    """Live case (2026-11-22): hygiene abandoned the inline wait, then
    preflight and pre-API both lost the lock to the detached hygiene
    worker and the turn sent a ~344k-token request over a 272k window.
    A lock-skip on an over-window request now ends the turn instead."""
    from agent.turn_context import PreflightCompressionLockDeferred

    with pytest.raises(PreflightCompressionLockDeferred, match="already running"):
        _preflight(_lock_skipping_agent(context_length=131_072), 356_113)

    # Over threshold but inside the window: the defer stands, no raise —
    # the request is sent as-is, exactly as a lock-skip always behaved.
    outcome = _preflight(_lock_skipping_agent(context_length=1_000_000), 356_113)
    assert outcome.blocked is False
    assert outcome.messages == [{"role": "user", "content": "hi"}]
    # Window unresolvable (0): never guess — the previous behaviour stands.
    assert _preflight(_lock_skipping_agent(context_length=0), 356_113).blocked is False


def test_lock_deferred_preflight_maps_to_soft_deferred_result():
    """The lock-deferred preflight must end as ``compression_deferred``,
    never ``compression_exhausted`` — the gateway rotates the session on
    exhaustion, and a lock-skip is the opposite case: another path is
    shrinking the same session concurrently (#69870 / #49874)."""
    from agent.conversation_loop import _preflight_timeout_result
    from agent.turn_context import PreflightCompressionLockDeferred

    agent = SimpleNamespace(_compression_skipped_due_to_lock="pid=4242:holder", session_id="s1")
    deferred = _preflight_timeout_result(
        agent, PreflightCompressionLockDeferred("already running"),
        [{"role": "user", "content": "hi"}],
    )
    assert deferred.get("compression_deferred") is True
    assert not deferred.get("compression_exhausted")
    assert deferred.get("failed") is False
    assert deferred.get("completed") is False
    assert "already running" in deferred.get("final_response", "")

    # The plain timeout keeps its exhaustion shape.
    timed_out = _preflight_timeout_result(
        agent, PreflightCompressionTimedOut("timed out"),
        [{"role": "user", "content": "hi"}],
    )
    assert timed_out.get("compression_exhausted") is True
    assert not timed_out.get("compression_deferred")


def test_over_window_wait_is_bounded_by_one_inactivity_budget():
    msgs = [{"role": "user", "content": "x"}]

    def trickle(fence):
        end = time.monotonic() + 5
        while time.monotonic() < end and not (fence.is_cancelled or fence.deadline_exceeded):
            fence.touch_progress()  # a token every 50ms: never idle, never committing
            time.sleep(0.05)
        return msgs, ""

    def waited(*, request_exceeds_window):
        started = time.monotonic()
        result = run_compress_context_with_progress_timeout(
            worker=trickle, messages=msgs, system_prompt_fallback="sys", idle_timeout_seconds=0.4,
            total_ceiling_seconds=2.0, request_exceeds_window=request_exceeds_window, stall_fallback=False,
        )
        assert result[0] is msgs
        return time.monotonic() - started

    assert waited(request_exceeds_window=True) < 1.5   # one idle budget (0.4s), not the 2s ceiling
    assert waited(request_exceeds_window=False) >= 1.9  # a fitting request keeps the full ceiling
