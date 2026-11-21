"""Runtime anti-stall guards (agent.stall_guards) — no-limits build.

The identical-call loop breaker is REMOVED: ``ToolCallGuardrailController.observe_call``
always returns an empty observation (no notice, no stub, no halt), and
``AIAgent._append_guardrail_observation`` passes tool results through
unchanged. Repeated identical calls are never warned about, blocked, stubbed,
or halted.

The said-continue-but-stopped detector — ``trailing_continue_intent`` — is
unchanged and still covered below.

These assert behavior contracts, not message snapshots.
"""

from agent.agent_runtime_helpers import trailing_continue_intent
from agent.tool_guardrails import (
    IDENTICAL_RESULT_STUB_MIN_CHARS,
    STALL_GUARD_IDENTICAL_CALL_THRESHOLD,
    ToolCallGuardrailController,
)


def _observe_n(controller, n, tool="web_search", args=None, result="same result"):
    notices = []
    for _ in range(n):
        notices.append(
            controller.observe_call(tool, args or {"query": "x"}, result).notice
        )
    return notices


# ── identical-call loop breaker (REMOVED under no-limits) ───────────────────
# observe_call always returns an empty observation: no notice, no stub, no
# halt, however many identical calls repeat. These pin that contract.


def test_no_notice_on_third_consecutive_identical_call_and_result():
    c = ToolCallGuardrailController()
    notices = _observe_n(c, 3)
    assert notices == [None, None, None]


def test_no_notice_past_threshold():
    c = ToolCallGuardrailController()
    notices = _observe_n(c, 4)
    assert all(n is None for n in notices)


def test_does_not_fire_when_arguments_differ():
    c = ToolCallGuardrailController()
    for i in range(5):
        notice = c.observe_call(
            "web_search", {"query": f"q{i}"}, "same result"
        ).notice
        assert notice is None


def test_does_not_fire_when_results_differ():
    c = ToolCallGuardrailController()
    for i in range(5):
        notice = c.observe_call(
            "terminal", {"command": "poll-status"}, f"output {i}"
        ).notice
        assert notice is None


def test_streak_resets_when_a_different_call_intervenes():
    c = ToolCallGuardrailController()
    assert _observe_n(c, 2)[-1] is None
    # Different tool breaks the consecutive streak.
    assert c.observe_call("read_file", {"path": "/a"}, "data").notice is None
    # Two more of the original are a fresh streak of 2 — still no notice.
    assert all(n is None for n in _observe_n(c, 2))


def test_arg_canonicalization_ignores_key_order():
    # The signature hash is still canonical (key order never matters), but
    # observe_call no longer notices repeats of any kind.
    from agent.tool_guardrails import ToolCallSignature

    assert ToolCallSignature.from_call("t", {"a": 1, "b": 2}) == ToolCallSignature.from_call(
        "t", {"b": 2, "a": 1}
    )
    c = ToolCallGuardrailController()
    r = "same"
    assert c.observe_call("t", {"a": 1, "b": 2}, r).notice is None
    assert c.observe_call("t", {"b": 2, "a": 1}, r).notice is None
    assert c.observe_call("t", {"a": 1, "b": 2}, r).notice is None


def test_allowlisted_pollers_never_fire():
    c = ToolCallGuardrailController()
    for tool in ("process_manage", "vendor_get_result", "job_poll"):
        for _ in range(STALL_GUARD_IDENTICAL_CALL_THRESHOLD + 2):
            assert c.observe_call(tool, {"id": "j1"}, "Generating").notice is None




def test_resets_per_turn():
    c = ToolCallGuardrailController()
    assert _observe_n(c, 2)[-1] is None
    c.reset_for_turn()
    # Streak restarted — and still no notice on the third identical call.
    assert all(n is None for n in _observe_n(c, 3))


def test_never_blocks_execution():
    # The guard is observational only: before_call still allows the call even
    # after the notice fires repeatedly.
    c = ToolCallGuardrailController()
    _observe_n(c, 6)
    decision = c.before_call("web_search", {"query": "x"})
    assert decision.allows_execution


# ── result-append integration (AIAgent._append_guardrail_observation) ─────


def _fake_agent(stall_guards=True):
    from types import SimpleNamespace

    from run_agent import AIAgent

    agent = SimpleNamespace(
        _tool_guardrails=ToolCallGuardrailController(),
        _stall_guards=stall_guards,
        _tool_guardrail_halt_decision=None,
    )
    agent._stall_guards_enabled = lambda: AIAgent._stall_guards_enabled(agent)
    agent._set_tool_guardrail_halt = (
        lambda decision: AIAgent._set_tool_guardrail_halt(agent, decision)
    )
    agent._append = (
        lambda name, args, result, failed=False, tool_call_id="": (
            AIAgent._append_guardrail_observation(
                agent, name, args, result, failed=failed, tool_call_id=tool_call_id
            )
        )
    )
    return agent


def test_no_notice_appended_to_third_identical_result():
    agent = _fake_agent()
    args = {"query": "hermes"}
    r1 = agent._append("web_search", args, "results")
    r2 = agent._append("web_search", args, "results")
    r3 = agent._append("web_search", args, "results")
    assert r1 == r2 == r3 == "results"  # no notice appended, result preserved


def test_config_gate_disables_notice():
    agent = _fake_agent(stall_guards=False)
    args = {"query": "hermes"}
    for _ in range(4):
        result = agent._append("web_search", args, "results")
    assert "hermes note" not in result


def test_repeated_identical_results_pass_through_unannotated():
    # No-limits: there is no warning suffix and no notice any more — the raw
    # result passes through verbatim on every repeat.
    agent = _fake_agent()
    args = {"path": "/tmp/x"}
    outs = [agent._append("read_file", args, "contents") for _ in range(3)]
    assert outs == ["contents"] * 3


# ── result-reference stubbing (REMOVED under no-limits) ─────────────────────
# Duplicate byte-identical results are never stubbed: every repeat enters
# context whole.


_BIG = "x" * IDENTICAL_RESULT_STUB_MIN_CHARS  # exactly at the stub threshold


def test_second_identical_call_returns_full_result():
    agent = _fake_agent()
    args = {"query": "hermes"}
    r1 = agent._append("web_search", args, _BIG, tool_call_id="call_1")
    r2 = agent._append("web_search", args, _BIG, tool_call_id="call_2")
    assert r1 == _BIG  # first occurrence always enters context whole
    assert r2 == _BIG  # no stub: the duplicate payload enters context whole too


def test_no_stub_when_fresh_result_differs():
    agent = _fake_agent()
    args = {"query": "hermes"}
    agent._append("web_search", args, _BIG, tool_call_id="call_1")
    changed = "y" + _BIG
    r2 = agent._append("web_search", args, changed, tool_call_id="call_2")
    assert r2 == changed  # changed result flows through whole


def test_changed_and_repeated_results_all_pass_through_whole():
    agent = _fake_agent()
    args = {"id": "job"}
    agent._append("web_search", args, _BIG, tool_call_id="a")
    changed = _BIG + "done"
    r2 = agent._append("web_search", args, changed, tool_call_id="b")
    assert r2 == changed
    r3 = agent._append("web_search", args, changed, tool_call_id="c")
    assert r3 == changed  # no stub even for the repeat of the new result


def test_no_stub_below_min_chars():
    agent = _fake_agent()
    small = "x" * (IDENTICAL_RESULT_STUB_MIN_CHARS - 1)
    args = {"query": "hermes"}
    agent._append("web_search", args, small, tool_call_id="c1")
    r2 = agent._append("web_search", args, small, tool_call_id="c2")
    assert "byte-identical" not in r2
    assert r2.startswith(small)  # full payload kept (pre-existing warning suffix allowed)


def test_no_stub_for_error_results():
    agent = _fake_agent()
    err = "Error executing tool: " + _BIG
    args = {"command": "boom"}
    agent._append("terminal", args, err, failed=True, tool_call_id="c1")
    r2 = agent._append("terminal", args, err, failed=True, tool_call_id="c2")
    assert "byte-identical" not in r2
    assert r2.startswith(err)  # models must see fresh errors whole


def test_pollers_get_full_results_never_stubbed_or_noticed():
    agent = _fake_agent()
    args = {"id": "job1"}
    results = [
        agent._append("vendor_get_result", args, _BIG, tool_call_id=f"c{i}")
        for i in range(4)
    ]
    assert all(r == _BIG for r in results)  # full payload every time


def test_third_identical_call_returns_full_result_no_stub_no_notice():
    agent = _fake_agent()
    args = {"query": "hermes"}
    agent._append("web_search", args, _BIG, tool_call_id="c1")
    agent._append("web_search", args, _BIG, tool_call_id="c2")
    r3 = agent._append("web_search", args, _BIG, tool_call_id="c3")
    assert r3 == _BIG


def test_persisted_result_never_referenced_by_stub():
    agent = _fake_agent()
    args = {"query": "big"}
    agent._tool_guardrails.record_persisted_result(
        "c1", "/home/u/.hermes/cache/spillover/c1.txt"
    )
    agent._append("web_search", args, _BIG, tool_call_id="c1")
    r2 = agent._append("web_search", args, _BIG, tool_call_id="c2")
    assert r2 == _BIG
    assert "/home/u/.hermes/cache/spillover/c1.txt" not in r2


def test_no_args_summary_stub_on_duplicate_results():
    agent = _fake_agent()
    args = {"query": "hermes result stubbing", "limit": 5}
    agent._append("web_search", args, _BIG, tool_call_id="c1")
    r2 = agent._append("web_search", args, _BIG, tool_call_id="c2")
    assert r2 == _BIG  # full payload; no stub, no args preview


def test_no_stub_even_for_huge_args():
    # No-limits: duplicate results are never stubbed, so there is no args
    # preview to truncate — observe_call returns an empty observation.
    c = ToolCallGuardrailController()
    args = {"query": "q" * 500}
    assert c.observe_call("web_search", args, _BIG, tool_call_id="c1").stub is None
    assert c.observe_call("web_search", args, _BIG, tool_call_id="c2").stub is None


def test_config_off_disables_stub():
    agent = _fake_agent(stall_guards=False)
    args = {"query": "hermes"}
    agent._append("web_search", args, _BIG, tool_call_id="c1")
    r2 = agent._append("web_search", args, _BIG, tool_call_id="c2")
    assert "byte-identical" not in r2
    assert r2.startswith(_BIG)


def test_streak_reset_by_different_call_means_next_identical_is_full():
    agent = _fake_agent()
    args = {"query": "hermes"}
    agent._append("web_search", args, _BIG, tool_call_id="c1")
    agent._append("read_file", {"path": "/a"}, "other", tool_call_id="c2")
    r3 = agent._append("web_search", args, _BIG, tool_call_id="c3")
    assert "byte-identical" not in r3
    assert r3.startswith(_BIG)  # fresh streak — first occurrence full again


def test_multimodal_content_never_stubbed_and_breaks_streak():
    c = ToolCallGuardrailController()
    args = {"path": "/img.png"}
    assert c.observe_call("vision", args, _BIG, tool_call_id="c1").stub is None
    # Non-string (multimodal) results never form or extend a streak.
    obs = c.observe_call("vision", args, None, tool_call_id="c2")
    assert obs.stub is None
    assert c.observe_call("vision", args, _BIG, tool_call_id="c3").stub is None


def test_extract_persisted_path_round_trip():
    # The stub's spillover reference is parsed from the <persisted-output>
    # block that maybe_persist_tool_result builds — assert the round trip.
    from tools.tool_result_storage import (
        _build_persisted_message,
        extract_persisted_path,
    )

    block = _build_persisted_message("preview", True, 50_000, "/tmp/spill/x.txt")
    assert extract_persisted_path(block) == "/tmp/spill/x.txt"
    assert extract_persisted_path("plain result") is None


# ── said-continue-but-stopped detector ─────────────────────────────────────


def test_detects_trailing_let_me_now():
    assert trailing_continue_intent("Found the config file. Let me now update it.")


def test_detects_trailing_i_will_now():
    assert trailing_continue_intent("The tests pass. I will now push the branch.")
    assert trailing_continue_intent("Good. I'll now run the linter")


def test_detects_trailing_next_i():
    assert trailing_continue_intent("Done with step one. Next, I check the logs")
    assert trailing_continue_intent("Step one complete. Next: I run the tests")


def test_ignores_intent_followed_by_more_content():
    # Intent phrase mid-message with substantive content after it — the model
    # already continued; nothing dangling.
    assert not trailing_continue_intent(
        "I will now explain the tradeoffs. First, caching: the design keeps "
        "the prefix stable. Second, alternation: roles must strictly alternate."
    )


def test_ignores_long_substantive_replies():
    long_reply = ("Here is the full analysis. " * 30) + "Let me now summarize."
    assert not trailing_continue_intent(long_reply)


def test_ignores_plain_final_answers():
    assert not trailing_continue_intent("The answer is 42.")
    assert not trailing_continue_intent("All tests pass and the branch is pushed.")
    assert not trailing_continue_intent("")
    assert not trailing_continue_intent(None)


def test_ignores_conversational_future_offers():
    # "I will" without the immediate-action shape must not trip the guard.
    assert not trailing_continue_intent(
        "I can help with that tomorrow if you'd like."
    )
    assert not trailing_continue_intent(
        "If you want, I will happily review the PR once CI is green. Just say so!"
    )


# ── batch-cycle loop breaker (port of can1357/oh-my-pi#10521) ───────────────


def test_repeating_two_call_cycle_never_notices_or_halts():
    """No-limits: an A,B,A,B,... cycle of identical (args, result) pairs is
    never noticed and never halts — the cycle detector is a no-op."""
    from agent.tool_guardrails import ToolCallGuardrailConfig

    c = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    pairs = (({"command": "make build"}, "error: X\n"),
             ({"command": "tail -5 build.log"}, "still broken\n"))
    for _ in range(30):
        for args, result in pairs:
            observation = c.observe_call("terminal", args, result)
            assert observation.notice is None
            assert observation.stub is None
            assert observation.kind is None
    assert c.halt_decision is None


def test_cycle_guard_stays_silent_for_progressing_and_poller_cycles():
    """A cycle whose results change every lap is real work; a cycle made only
    of poller-exempt tools is legitimate waiting. Neither may fire."""
    from agent.tool_guardrails import ToolCallGuardrailConfig

    progressing = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    for i in range(20):
        for args in ({"command": "make"}, {"command": "tail log"}):
            assert progressing.observe_call("terminal", args, f"output {i}").notice is None
    assert progressing.halt_decision is None

    pollers = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    for _ in range(20):
        for args in ({"action": "poll", "session_id": "a"}, {"action": "poll", "session_id": "b"}):
            assert pollers.observe_call("process_manage", args, "running").notice is None
    assert pollers.halt_decision is None


# ── promoted-reasoning plan-tail detector (#111761) ─────────────────────────


def test_promoted_reasoning_detector_catches_first_person_plan_tails():
    from agent.agent_runtime_helpers import promoted_reasoning_announces_action

    # Verbatim tails from the #111761 thread — none match the narrow visible-content detector.
    for tail in (
        "Let me batch the terminal calls and run them in parallel.",
        "...Let me load the doctrine skill first, then run checks.",
        "Initial hypothesis: the config is stale. I need to check the log.",
        "I'm going to run the tests now",
        "嗯，长度合适。Let me check the file first.",
    ):
        assert not trailing_continue_intent(tail), tail
        assert promoted_reasoning_announces_action(tail), tail
    # Long monologue: only the tail decides (no 400-char cap on this path).
    assert promoted_reasoning_announces_action(("Thinking about the task. " * 60) + "Let me read the file.")


def test_promoted_reasoning_detector_ignores_stated_answers():
    from agent.agent_runtime_helpers import promoted_reasoning_announces_action

    for text in (
        "The answer is 42.",
        "Let me check the arithmetic. 6 times 7 is 42, so the answer is 42.",
        "If you want, I will happily review the PR once CI is green. Just say so!",
        "structured reasoning answer",
        "",
        None,
    ):
        assert not promoted_reasoning_announces_action(text), text


def test_promoted_reasoning_detector_catches_thai_plan_tails():
    from agent.agent_runtime_helpers import promoted_reasoning_announces_action

    # Thai is unsegmented (no spaces between words), so the tail-boundary check falls back to
    # sentence punctuation, an em/en dash, or a run of ellipsis dots (#116495).
    for tail in (
        "พร้อมแล้ว — จะให้ผมส่ง JANUS...",  # verbatim tail from the issue
        "ผมจะตรวจโค้ดให้เดี๋ยวนี้เลยครับ",
        "เข้าใจแล้ว. ต่อไปจะลองรันเทสต์ดูครับ",
        "คิดอยู่… ขอเริ่มจากไฟล์แรกก่อนนะครับ",
        "บั๊กอยู่ตรงนี้\nจะแก้ให้เลยครับ",
    ):
        assert promoted_reasoning_announces_action(tail), tail


def test_promoted_reasoning_detector_ignores_thai_stated_answers():
    from agent.agent_runtime_helpers import promoted_reasoning_announces_action

    for text in (
        "คำตอบคือ 42 ครับ",  # "the answer is 42"
        "ตรวจสอบแล้ว. คำตอบคือ 42 ครับ",
        "พรุ่งนี้จะฝนตกทั่วประเทศ",  # "tomorrow it will rain" — not a first-person action verb
    ):
        assert not promoted_reasoning_announces_action(text), text
