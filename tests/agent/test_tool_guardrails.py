"""Pure tool-call guardrail primitive tests."""

import json

import pytest

from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    canonical_tool_args,
    classify_tool_failure,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_tool_call_signature_hashes_canonical_nested_unicode_args_without_exposing_raw_args():
    args_a = {
        "z": [{"β": "☤", "a": 1}],
        "a": {"y": 2, "x": "secret-token-value"},
    }
    args_b = {
        "a": {"x": "secret-token-value", "y": 2},
        "z": [{"a": 1, "β": "☤"}],
    }

    assert canonical_tool_args(args_a) == canonical_tool_args(args_b)
    sig_a = ToolCallSignature.from_call("web_search", args_a)
    sig_b = ToolCallSignature.from_call("web_search", args_b)

    assert sig_a == sig_b
    assert len(sig_a.args_hash) == 64
    metadata = sig_a.to_metadata()
    assert metadata == {"tool_name": "web_search", "args_hash": sig_a.args_hash}
    assert "secret-token-value" not in json.dumps(metadata)
    assert "☤" not in json.dumps(metadata)




def test_config_parses_nested_warn_and_hard_stop_thresholds():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "warnings_enabled": False,
            "hard_stop_enabled": True,
            "warn_after": {
                "exact_failure": 3,
                "same_tool_failure": 4,
                "idempotent_no_progress": 5,
            },
            "hard_stop_after": {
                "exact_failure": 6,
                "same_tool_failure": 7,
                "idempotent_no_progress": 8,
            },
        }
    )

    assert cfg.warnings_enabled is False
    assert cfg.hard_stop_enabled is True
    assert cfg.exact_failure_warn_after == 3
    assert cfg.same_tool_failure_warn_after == 4
    assert cfg.no_progress_warn_after == 5
    assert cfg.exact_failure_block_after == 6
    assert cfg.same_tool_failure_halt_after == 7
    assert cfg.no_progress_block_after == 8


def test_gateway_platform_defaults_to_hard_stop_without_changing_interactive_defaults():
    interactive_configs = [
        ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        for platform in ("cli", "tui", "desktop", "acp")
    ]
    telegram_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="telegram")
    cron_cfg = ToolCallGuardrailConfig.from_mapping({}, platform="cron")

    assert all(cfg.hard_stop_enabled is False for cfg in interactive_configs)
    assert telegram_cfg.hard_stop_enabled is True
    assert cron_cfg.hard_stop_enabled is True


def test_non_interactive_hard_stop_can_be_disabled_explicitly():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {"non_interactive_hard_stop_enabled": False},
        platform="telegram",
    )

    assert cfg.hard_stop_enabled is False
    assert cfg.non_interactive_hard_stop_enabled is False


def test_default_repeated_identical_failed_call_always_allows():
    # No-limits: repeated identical failures are never warned about, blocked,
    # or halted — every decision is allow.
    controller = ToolCallGuardrailController()
    args = {"query": "same"}

    decisions = []
    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decisions.append(
            controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        )

    assert all(d.action == "allow" for d in decisions)
    assert all(d.code == "allow" for d in decisions)
    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None


def test_hard_stop_config_no_longer_blocks_repeated_exact_failure():
    # No-limits: even with hard_stop_enabled, repeated exact failures never
    # warn or block — before_call always allows and no halt is recorded.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=True,
            exact_failure_warn_after=2,
            exact_failure_block_after=2,
            same_tool_failure_halt_after=99,
        )
    )
    args = {"query": "same"}

    for _ in range(5):
        assert controller.before_call("web_search", args).action == "allow"
        decision = controller.after_call("web_search", args, '{"error":"boom"}', failed=True)
        assert decision.action == "allow"
        assert decision.code == "allow"

    assert controller.before_call("web_search", args).action == "allow"
    assert controller.halt_decision is None














def test_skill_read_tools_never_block_repeated_identical_success_output():
    # No-limits: idempotent read tools returning the same result are never
    # warned about or blocked, even under a hard-stop config.
    cases = [
        (
            "skill_view",
            {"name": "gui-agent-ml-operations"},
            '{"success":true,"name":"gui-agent-ml-operations","content":"same"}',
        ),
        (
            "skills_list",
            {"category": "mlops"},
            '{"success":true,"skills":[{"name":"gui-agent-ml-operations"}]}',
        ),
    ]

    for tool_name, args, result in cases:
        controller = ToolCallGuardrailController(
            ToolCallGuardrailConfig(
                hard_stop_enabled=True,
                no_progress_warn_after=2,
                no_progress_block_after=2,
            )
        )

        for _ in range(3):
            assert controller.before_call(tool_name, args).action == "allow"
            decision = controller.after_call(tool_name, args, result, failed=False)
            assert decision.action == "allow"
            assert decision.code == "allow"
        assert controller.halt_decision is None


def test_mutating_or_unknown_tools_are_not_blocked_for_repeated_identical_success_output_by_default():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(no_progress_warn_after=2, no_progress_block_after=2)
    )

    for _ in range(3):
        assert controller.before_call("write_file", {"path": "/tmp/x", "content": "x"}).action == "allow"
        assert controller.after_call("write_file", {"path": "/tmp/x", "content": "x"}, "ok", failed=False).action == "allow"
        assert controller.before_call("custom_tool", {"x": 1}).action == "allow"
        assert controller.after_call("custom_tool", {"x": 1}, "ok", failed=False).action == "allow"


def test_identical_call_streak_never_halts_any_tool_even_when_hard_stop_enabled():
    # No-limits: observe_call is a no-op — a model replaying the same
    # successful terminal/skill_view call with a byte-identical result never
    # produces a notice, a stub, or a halt, even under hard_stop.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"command": "hermes config get memory.provider"}
    for _ in range(6):
        controller.after_call("terminal", args, "local\n", failed=False)
        observation = controller.observe_call("terminal", args, "local\n", failed=False)
        assert observation.notice is None
        assert observation.stub is None
        assert observation.kind is None
    assert controller.halt_decision is None


def test_identical_call_streak_never_halts_when_hard_stop_disabled_or_for_pollers():
    soft = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=False, no_progress_block_after=2)
    )
    for _ in range(6):
        soft.observe_call("terminal", {"command": "ls"}, "a\nb\n", failed=False)
    assert soft.halt_decision is None  # notice-only in interactive sessions

    hard = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    )
    for _ in range(6):
        hard.observe_call("process_manage", {"action": "poll", "session_id": "p1"}, "running", failed=False)
    assert hard.halt_decision is None  # an unchanged poll is legitimate progress

    # A changed result resets the streak.
    for i in range(6):
        hard.observe_call("terminal", {"command": "date"}, f"t{i}", failed=False)
    assert hard.halt_decision is None






# ── Per-turn runaway-loop caps (Claude Code v2.1.212, Week 29) ──────────────

from agent.tool_guardrails import LoopCapConfig






def test_loop_cap_zero_disables_and_junk_falls_back():
    # 0 is a legitimate "unlimited" value; negatives / junk fall back to default.
    assert LoopCapConfig.from_mapping({"max_web_searches": 0}).max_web_searches == 0
    assert LoopCapConfig.from_mapping({"max_web_searches": -5}).max_web_searches == LoopCapConfig().max_web_searches
    assert LoopCapConfig.from_mapping({"max_subagents": "nope"}).max_subagents == LoopCapConfig().max_subagents


def test_web_search_cap_no_longer_blocks_after_limit():
    # No-limits: the per-turn web-search cap is unenforced — searches past
    # the configured limit still execute, with or without hard_stop.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(
            hard_stop_enabled=False,
            loop_caps=LoopCapConfig(max_web_searches=3),
        )
    )
    for i in range(6):
        decision = controller.before_call("web_search", {"query": f"q{i}"})
        assert decision.action == "allow"
        assert decision.code == "allow"
    assert controller.halt_decision is None












# ── Legitimate flows must survive hard stops (Teknium, Sep 2026) ────────────
# No-limits: hard stops are gone, so these flows are unconditionally allowed —
# edit -> re-run loops, diagnostic sweeps of distinct red commands, browser
# retry-after-action, and the pure replay (same call, nothing changed between
# attempts) alike. Nothing here may warn, block, or halt.

_HARD = lambda: ToolCallGuardrailController(
    ToolCallGuardrailConfig(hard_stop_enabled=True)
)
_PYTEST = {"command": "pytest tests/test_x.py -q"}
_RED = '{"output": "1 failed", "exit_code": 1}'


def _run_red(c, args=_PYTEST):
    assert c.before_call("terminal", args).allows_execution
    return c.after_call("terminal", args, _RED, failed=True)


def test_fix_retest_loop_is_never_hard_stopped():
    c = _HARD()
    for i in range(12):
        d = _run_red(c)
        assert not d.should_halt, f"halted on red run {i + 1}"
        # the model edits between runs — a landed mutation is progress
        c.after_call("patch", {"path": "x.py", "old_string": "a", "new_string": f"b{i}"},
                     '{"success": true, "diff": "..."}', failed=False)
    assert c.halt_decision is None
    assert c.before_call("terminal", _PYTEST).allows_execution


def test_pure_replay_with_no_intervening_change_is_now_allowed():
    # No-limits: a pure replay (same call, nothing changed between attempts)
    # is no longer blocked — it executes like any other call.
    c = _HARD()
    _run_red(c)
    d = c.before_call("terminal", _PYTEST)
    assert d.action == "allow" and d.code == "allow"
    assert c.halt_decision is None


def test_repeated_failures_around_a_mutation_are_all_allowed():
    # No-limits: failures before and after an intervening edit are all
    # allowed — there is no replay streak left to reset.
    c = _HARD()
    _run_red(c)
    c.after_call("write_file", {"path": "x.py", "content": "y"}, '{"bytes_written": 1}', failed=False)
    for _ in range(5):
        assert c.before_call("terminal", _PYTEST).allows_execution
        d = c.after_call("terminal", _PYTEST, _RED, failed=True)
        assert d.action == "allow"
    assert c.before_call("terminal", _PYTEST).action == "allow"
    assert c.halt_decision is None


def test_distinct_failing_terminal_commands_never_warn_or_halt():
    # No-limits: a diagnostic sweep of distinct failing commands never warns
    # or halts — and neither does a non-tolerant tool failing 8 distinct ways.
    c = _HARD()
    for i in range(12):
        args = {"command": f"grep -q needle{i} haystack.txt"}
        d = c.after_call("terminal", args, _RED, failed=True)
        assert d.action == "allow", f"warned on distinct command #{i + 1}"
        assert not d.should_halt, f"halted on distinct command #{i + 1}"
    assert c.halt_decision is None
    c2 = _HARD()
    last = None
    for i in range(8):
        last = c2.after_call("send_message", {"to": f"u{i}"}, '{"error": "no route"}', failed=True)
    assert last.action == "allow"
    assert not last.should_halt
    assert c2.halt_decision is None


def test_browser_retry_after_action_is_not_a_replay():
    c = _HARD()
    nav = {"url": "https://example.test/app"}
    for _ in range(8):
        assert c.before_call("browser_navigate", nav).allows_execution
        c.after_call("browser_navigate", nav, '{"error": "timeout"}', failed=True)
        c.after_call("browser_click", {"selector": "#retry"}, '{"ok": true}', failed=False)
    assert c.halt_decision is None


def test_supervised_task_platforms_keep_warning_only_default():
    for platform in ("subagent", "api_server", "cli"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is False, platform
    for platform in ("telegram", "discord", "cron", "kanban"):
        cfg = ToolCallGuardrailConfig.from_mapping({}, platform=platform)
        assert cfg.hard_stop_enabled is True, platform


def test_harness_refusals_are_not_tool_failures_on_either_classifier(tmp_path):
    """Every loop refusal the file tools emit (read dedup block, consecutive-read block,
    repeated-search block) carries `"error"` for the model's benefit -- exactly what the
    substring tests key on. Neither `classify_tool_failure` nor the executor's live seam
    `_detect_tool_failure` may count them, or the cheap refusal feeds the streak that
    fires `repeated_exact_failure_block` over calls that never failed."""
    from agent.display import _detect_tool_failure
    from tools.file_tools import _dedup_stub_or_block, read_file_tool, search_tool

    target = tmp_path / "responses.ts"
    target.write_text("export const x = 1;\n" * 30, encoding="utf-8")

    task = {"dedup_hits": {}}
    for _ in range(3):
        dedup_block = _dedup_stub_or_block(task, (str(target), 1, 999), str(target))
    consecutive_block = [read_file_tool(str(target), offset=1, limit=5, task_id="t-read") for _ in range(4)][-1]
    search_block = [search_tool("const x", path=str(tmp_path), task_id="t-search") for _ in range(4)][-1]

    for refusal in (dedup_block, consecutive_block, search_block):
        assert json.loads(refusal)["error"].startswith("BLOCKED"), refusal
        assert classify_tool_failure("read_file", refusal) == (False, ""), refusal
        assert _detect_tool_failure("read_file", refusal) == (False, ""), refusal


def test_a_real_tool_error_is_still_a_failure():
    """The exemption is keyed on the marker, not on the word: a body that
    genuinely failed still counts, or the streak that stops a real loop is gone."""
    from agent.display import _detect_tool_failure

    real = '{"error": "ENOENT: no such file"}'
    assert classify_tool_failure("read_file", real)[0] is True
    assert _detect_tool_failure("read_file", real)[0] is True
    # The marker is only honoured as the literal boolean, never as truthy prose.
    assert classify_tool_failure("read_file", '{"error": "x", "guardrail_refusal": "yes"}')[0] is True


def test_execute_code_replay_with_volatile_metadata_never_notices_or_halts():
    # No-limits: observe_call is a no-op, so execute_code replays whose
    # results differ only in volatile metadata (execution_count,
    # duration_seconds) produce no notice, no stub, and no halt.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=5)
    )
    args = {"code": "import subprocess\nprint(subprocess.run(['true']).returncode) if False else None"}

    def result(n):
        return json.dumps({
            "status": "success", "output": "", "exit_code": 0, "tool_calls_made": 0,
            "duration_seconds": 1.0 + n / 100,
            "kernel": {"mode": "session", "reused": True, "execution_count": 100 + n, "state_reset": False},
            "stdout_truncated": False, "stdout_bytes_captured": 0,
        })

    for i in range(5):
        observation = controller.observe_call("execute_code", args, result(i), failed=False)
        assert observation.notice is None
        assert observation.stub is None
        assert observation.kind is None
    assert controller.halt_decision is None


def test_identical_streak_still_resets_when_real_output_changes():
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=3)
    )
    args = {"code": "print(1)"}
    for i in range(6):
        out = json.dumps({"status": "success", "output": f"line {i}", "duration_seconds": 1.0,
                          "kernel": {"execution_count": i}})
        controller.observe_call("execute_code", args, out, failed=False)
    assert controller.halt_decision is None, "different real output must not count as a replay"


def test_other_tools_keep_duration_and_execution_count_as_real_output():
    # Only execute_code's known metadata locations are volatile. For any other tool these keys
    # can be the actual answer (e.g. a job-status tool reporting how long a job ran), so results
    # that differ only there are different results and must not form an identical streak.
    controller = ToolCallGuardrailController(
        ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=3)
    )
    args = {"job": "build-42"}
    for i in range(6):
        out = json.dumps({"status": "done", "duration_seconds": 10 + i,
                          "stats": {"execution_count": 100 + i}})
        controller.observe_call("mcp_ci_job_status", args, out, failed=False)
    assert controller.halt_decision is None, "a real change in duration_seconds/execution_count was treated as a replay"


def test_execute_code_replay_never_notices_on_warn_only_desktop_config():
    # No-limits: on an interactive surface the appended notice is gone too —
    # repeated no-op print("...") cells produce no notice at all.
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({}, platform="desktop"))
    args = {"code": 'print("...")'}

    def result(n):
        return json.dumps({
            "status": "success", "output": "...\n", "exit_code": 0, "tool_calls_made": 0,
            "duration_seconds": 0.001 * n,
            "kernel": {"mode": "session", "reused": True, "execution_count": n, "state_reset": False},
            "stdout_truncated": False, "stdout_bytes_captured": 4, "stdout_bytes_total": 4,
            "stdout_bytes_omitted": 0,
        })

    observations = [
        controller.observe_call("execute_code", args, result(i), tool_call_id=f"c{i}")
        for i in range(1, 7)
    ]
    assert all(
        o.notice is None and o.stub is None and o.kind is None for o in observations
    ), observations
    assert controller.halt_decision is None, "warn-only surfaces must not halt"


@pytest.mark.parametrize("prior_ordinary_failures", [0, 2])
@pytest.mark.parametrize("result,throttled", [
    (json.dumps({"error": "Slack API error: ratelimited. Retry after 1 seconds."}), True),
    (json.dumps({"ok": False, "error": "busy", "retry_after": 30}), True),
    (json.dumps({"error": "channel_not_found"}), False),  # control: a real replay still warns
])
def test_rate_limited_retry_is_not_a_replay_and_never_bounded(result, throttled, prior_ordinary_failures):
    """No-limits: retrying a throttled read after its cooldown draws no
    warning, and an endpoint that never recovers is never halted or blocked
    by the guardrail — the turn is bounded only by the iteration budget.
    Throttled and ordinary failures now behave identically: always allow."""
    controller = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({}, platform="webhook"))
    tool, args = "mcp_slack_history", {"channel": "C123"}
    for _ in range(prior_ordinary_failures):
        controller.after_call(tool, args, json.dumps({"error": "upstream timeout"}))
    controller.after_call(tool, args, result)
    retry = controller.after_call(tool, args, result)
    assert retry.action == "allow"
    assert retry.code == "allow"
    for _ in range(10):
        assert controller.before_call(tool, args).allows_execution
        assert controller.after_call(tool, args, result).action == "allow"
    assert controller.halt_decision is None


def test_default_hard_stop_thresholds_are_one():
    """Approvals 5→1: the shipped defaults are one.

    The dataclass defaults, the empty-mapping parse, and the shipped
    ``DEFAULT_CONFIG`` section must agree: exact_failure_block_after and
    no_progress_block_after are 1. ``same_tool_failure`` stays 8 (never a 5).
    Note: under no-limits these thresholds gate nothing — enforcement was
    removed — this pins the shipped 5→1 values only.
    """
    for cfg in (
        ToolCallGuardrailConfig(),
        ToolCallGuardrailConfig.from_mapping({}, platform="telegram"),
        ToolCallGuardrailConfig.from_mapping(
            DEFAULT_CONFIG["tool_loop_guardrails"], platform="telegram"
        ),
    ):
        assert cfg.exact_failure_block_after == 1
        assert cfg.no_progress_block_after == 1
        assert cfg.same_tool_failure_halt_after == 8
