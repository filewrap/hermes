"""Reformat contracts for /goal, /heartbeat, /refine, /review, /subgoal,
/loop, /swarm, /login, /pause, /suggestions, /blueprint.

Behaviour contracts only: every reformed Telegram response carries a
``## `` heading, labelled ``- **Label:** value`` rows (or list items),
sections separated by single blank lines, absent fields omitted (never a
placeholder), and no flat wall of text. The plain-text fallback off
Telegram is unchanged — only the card text is reformatted (except /swarm's
non-Telegram snapshot and /blueprint's returned text, which are the reply
themselves). Pushed /login notices (link, code, completion) are pinned
elsewhere and stay byte-identical.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_busy import (
    _rich_blueprint_text,
    _rich_pause_text,
    _rich_suggestions_text,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.slash_commands_goals import (
    _rich_goal_text,
    _rich_heartbeat_text,
    _rich_loop_text,
    _rich_refine_text,
    _rich_review_text,
    _rich_subgoal_text,
)
from gateway.slash_commands_login import _rich_login_text
from gateway.slash_commands_swarm import _plain_text_snapshot
from hermes_cli import anon_auth

_EMOJI = set("\u23f3\u26a0\u23f8\u25b6\u2665\u21bb\u2299\u2713\u2697\u26d4\u23f8\ufe0f\u25b6\ufe0f")


def _assert_card_shape(text: str) -> None:
    """Shared shape invariants for one reformed card text."""
    assert text.startswith("## "), text
    assert "\n\n\n" not in text, text
    assert text == text.strip(), text
    assert not any(ch in text for ch in _EMOJI), text


def _card_html(adapter) -> str:
    card = adapter.send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs["html"]


def _source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester",
        chat_type="dm",
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform), message_id="m1")


def _make_runner():
    """Bare GatewayRunner with a fake Telegram adapter exposing send_command_card."""
    runner = object.__new__(GatewayRunner)
    card = AsyncMock(return_value=True)
    adapter = SimpleNamespace(send=AsyncMock(), send_command_card=card)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._reply_anchor_for_event = lambda event: None
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:u1"
    runner._running_agents = {}
    runner._cached_agent_for = lambda key: None
    return runner, adapter


# --- /goal -------------------------------------------------------------------

def test_goal_status_card_has_heading_labelled_rows_and_no_placeholders():
    text = _rich_goal_text("\u2299 Goal (active, 0/10 turns, 2 subgoals, contract): build the thing")
    _assert_card_shape(text)
    assert text.startswith("## Status\n\n")
    assert "- **Status:** active" in text
    assert "- **Goal:** build the thing" in text
    assert "- **Progress:** 0/10 turns" in text
    assert "- **Subgoals:** 2" in text
    assert "- **Contract:** set" in text


def test_goal_status_omits_absent_fields():
    text = _rich_goal_text("\u2299 Goal (active, 0/10 turns): build the thing")
    _assert_card_shape(text)
    assert "Subgoals" not in text
    assert "Gates" not in text
    assert "Contract" not in text
    assert "(none)" not in text and "none" not in text.lower()


def test_goal_set_card_sections_contract_and_previous_goal():
    text = _rich_goal_text(
        "\u2299 Goal set (10-turn budget): build the thing\n"
        "(replaced the previous goal)\n"
        "  was: old thing\n"
        "Completion contract:\n"
        "- Outcome: thing built\n"
        "- Verify: pytest -q\n"
        "After each turn, a judge model checks if the goal is done."
    )
    _assert_card_shape(text)
    assert text.startswith("## Goal set\n\n")
    assert "- **Goal:** build the thing" in text
    assert "- **Budget:** 10 turns" in text
    assert "- **Previous goal:** old thing" in text
    assert "\n\n## Completion contract\n\n- Outcome: thing built\n- Verify: pytest -q" in text
    assert "After each turn, a judge model checks" in text


def test_goal_gate_add_card_has_command_rows():
    text = _rich_goal_text(
        "⚿ Gate added: $ pytest -q (2 retries, 30s timeout). "
        "It must pass before the goal can complete."
    )
    _assert_card_shape(text)
    assert text.startswith("## Gate added\n\n")
    assert "- **Command:** `$ pytest -q`" in text
    assert "- **Retries:** 2" in text
    assert "- **Timeout:** 30s" in text


def test_goal_unknown_shape_keeps_text_under_heading():
    text = _rich_goal_text("Goals unavailable on this session.")
    _assert_card_shape(text)
    assert "Goals unavailable on this session." in text


# --- /heartbeat, /subgoal, /loop ----------------------------------------------

def test_heartbeat_status_card_rows_and_omitted_absent_fields():
    text = _rich_heartbeat_text("\u2665 Heartbeat (every 10m, next in ~300s, fired 2×): check the inbox")
    _assert_card_shape(text)
    assert "- **Status:** active" in text
    assert "- **Prompt:** check the inbox" in text
    assert "- **Interval:** every 10m" in text
    assert "- **Next:** ~300s" in text
    assert "- **Fired:** 2×" in text


def test_heartbeat_set_card_rows_and_note():
    text = _rich_heartbeat_text(
        "\u2665 Heartbeat set (every 10m): check the inbox\n"
        "Fires as a normal turn whenever this session is idle."
    )
    _assert_card_shape(text)
    assert text.startswith("## Heartbeat set\n\n")
    assert "- **Interval:** every 10m" in text
    assert "- **Prompt:** check the inbox" in text
    assert "Fires as a normal turn whenever this session is idle." in text


def test_unparseable_status_keeps_text_under_heading():
    # The shape the automation-cards test feeds in: no parenthetical to parse.
    text = _rich_heartbeat_text("Heartbeat: every 10m — check the inbox")
    _assert_card_shape(text)
    assert "Heartbeat" in text and "10m" in text


def test_subgoal_list_card_sections():
    text = _rich_subgoal_text(
        "\u2299 Goal (active, 3/10 turns, 2 subgoals): build the thing\n"
        "- 1. tests pass\n"
        "- 2. docs updated"
    )
    _assert_card_shape(text)
    assert text.startswith("## Subgoals\n\n")
    assert "- **Goal:** build the thing" in text
    assert "- **Progress:** 3/10 turns" in text
    assert "\n\n- 1. tests pass\n- 2. docs updated" in text


def test_subgoal_added_card_has_numbered_row():
    text = _rich_subgoal_text("\u2713 Added subgoal 3: update the docs")
    _assert_card_shape(text)
    assert text.startswith("## Subgoal added\n\n")
    assert "- **Subgoal 3:** update the docs" in text


def test_loop_set_card_sections_and_controls():
    text = _rich_loop_text(
        "\u21bb Loop set (every 5m): check the deploy\n"
        "(replaced the previous loop for this session)\n"
        "Runs 30 times, then stops.\n"
        "First wakeup fires now, then on the cadence above. "
        "Controls: /loop status · pause · resume · stop."
    )
    _assert_card_shape(text)
    assert text.startswith("## Loop set\n\n")
    assert "- **Cadence:** every 5m" in text
    assert "- **Prompt:** check the deploy" in text
    assert "- **Runs:** 30 times, then stops" in text
    assert "- **First wakeup:** fires now, then on the cadence above" in text
    assert "Replaced the previous loop for this session." in text
    assert "Controls: `/loop status` · `/loop pause` · `/loop resume` · `/loop stop`." in text


def test_loop_help_card_has_examples_and_controls():
    text = _rich_loop_text(
        "Usage: /loop [interval] <prompt> [--times N] [--until <condition>]\n"
        "  /loop 5m check the deploy status      — first run now, then every 5m\n"
        "  /loop keep fixing tests until green   — self-paced\n"
        "Controls: /loop status · /loop pause · /loop resume · /loop stop\n"
        "The loop also stops itself when the agent replies with `LOOP_COMPLETE`."
    )
    _assert_card_shape(text)
    assert text.startswith("## Loop usage\n\n")
    assert "- `/loop 5m check the deploy status` — first run now, then every 5m" in text
    assert "Controls: `/loop status` · `/loop pause` · `/loop resume` · `/loop stop`" in text


def test_loop_status_card_rows():
    text = _rich_loop_text("\u21bb Loop (active, every 5m, 0/30 runs, next in 4m): poll CI")
    _assert_card_shape(text)
    assert "- **Status:** active" in text
    assert "- **Prompt:** poll CI" in text
    assert "- **Cadence:** every 5m" in text
    assert "- **Progress:** 0/30 runs" in text
    assert "- **Next:** 4m" in text


# --- /refine, /review ---------------------------------------------------------

def test_refine_started_card_with_focus_row():
    text = _rich_refine_text(
        "\u2697 Reviewing this conversation in the background (focus: memory) — "
        "any memory/skill updates will be reported when done."
    )
    _assert_card_shape(text)
    assert text.startswith("## Refine started\n\n")
    assert "Reviewing this conversation in the background — " in text
    assert "- **Focus:** memory" in text


def test_refine_without_focus_omits_focus_row():
    text = _rich_refine_text(
        "\u2697 Reviewing this conversation in the background — "
        "any memory/skill updates will be reported when done."
    )
    _assert_card_shape(text)
    assert "Focus" not in text


def test_review_completed_card_with_results_fence():
    text = _rich_review_text(
        'Review completed synchronously on nous/welcome (focus: the API) — results:\n'
        '{"verdict": "ok"}'
    )
    _assert_card_shape(text)
    assert text.startswith("## Review completed\n\n")
    assert "- **Model:** `nous/welcome`" in text
    assert "- **Focus:** the API" in text
    assert "\n\n## Results\n\n```\n{\"verdict\": \"ok\"}\n```" in text


def test_review_started_card_keeps_note():
    text = _rich_review_text("Review started. Results will return here.")
    _assert_card_shape(text)
    assert text.startswith("## Review started\n\n")
    assert "Review started. Results will return here." in text


# --- /swarm -------------------------------------------------------------------

def _entry(sid, status="running", kind="kanban", goal="durable swarm", workers=()):
    return {
        "durable_session_id": sid, "status": status, "kind": kind,
        "goal": goal, "workers": list(workers),
    }


def test_swarm_snapshot_heading_rows_and_italic_footer():
    text = _plain_text_snapshot([
        _entry("t_root9", workers=[
            {"label": "worker-1", "status": "running", "detail": "building the widget"},
        ]),
        _entry("s2", status="waiting", kind="delegate", goal=""),
    ])
    assert text.startswith("## Agents and swarms\n\n")
    assert "\n\n\n" not in text
    assert "- `t_root9` · running · kanban · durable swarm" in text
    assert "  - worker-1: running · building the widget" in text
    assert "- `s2` · waiting · delegate" in text
    assert text.rstrip().endswith("__Page 1 of 1 • total 2__")


def test_swarm_snapshot_empty_keeps_guidance():
    text = _plain_text_snapshot([])
    assert text.startswith("## Agents and swarms\n\n")
    assert "No live agents" in text
    assert "delegate_task appear here while they run" in text


def test_swarm_snapshot_truncates_at_twelve():
    text = _plain_text_snapshot([_entry(f"s{i}") for i in range(13)])
    assert "Showing 12 of 13." in text
    assert "- `s11`" in text
    assert "- `s12`" not in text
    assert text.rstrip().endswith("__Page 1 of 1 • total 13__")


# --- /login -------------------------------------------------------------------

def test_login_replies_get_outcome_headings_and_keep_text():
    cases = {
        anon_auth.LOGIN_DM_ONLY: "## Direct message required",
        anon_auth.LOGIN_NOT_ALLOWED: "## Operator only",
        anon_auth.LOGIN_BUSY_ELSEWHERE: "## Sign-in already running",
        anon_auth.UPGRADE_ALREADY_SIGNED_IN: "## Already signed in",
        anon_auth.UPGRADE_START: "## Sign in",
    }
    for original, heading in cases.items():
        text = _rich_login_text(original)
        assert text.startswith(f"{heading}\n\n"), text
        assert text.endswith(original), text


@pytest.mark.asyncio
async def test_login_refusal_card_has_heading_on_telegram():
    runner, adapter = _make_runner()
    # A group chat is refused before any sign-in starts; the card carries the heading.
    source = SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
        user_name="tester", chat_type="group",
    )
    event = MessageEvent(text="/login", source=source, message_id="m1")
    result = await runner._handle_login_command(event)
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Direct message required</h2>" in html
    assert "Sign in from a direct message with Hermes." in html


# --- /pause, /suggestions, /blueprint -----------------------------------------

def test_pause_card_reason_row_and_detail():
    text = _rich_pause_text(
        "\u23f8\ufe0f Paused (reason: deploy freeze). New cron/kanban/gateway work is on hold; "
        "in-flight work finishes normally. Use `/pause off` to resume."
    )
    _assert_card_shape(text)
    assert text.startswith("## Paused\n\n")
    assert "- **Reason:** deploy freeze" in text
    assert "New cron/kanban/gateway work is on hold" in text


def test_pause_without_reason_omits_reason_row():
    text = _rich_pause_text(
        "\u23f8\ufe0f Paused. New cron/kanban/gateway work is on hold; "
        "in-flight work finishes normally. Use `/pause off` to resume."
    )
    _assert_card_shape(text)
    assert "Reason" not in text


def test_pause_resumed_card_capitalises_detail():
    text = _rich_pause_text("\u25b6\ufe0f Resumed — new work is accepted again.")
    _assert_card_shape(text)
    assert text.startswith("## Resumed\n\n")
    assert "New work is accepted again." in text


def test_suggestions_list_card_rows_and_italic_footer():
    text = _rich_suggestions_text(
        "Suggested automations — `/suggestions accept N` or `dismiss N`:\n\n"
        "  1. Morning brief  [08:00]  (catalog)\n"
        "     A digest of overnight CI failures\n"
        "  2. Weekly review  [Mon 09:00]  (learned)"
    )
    _assert_card_shape(text)
    assert text.startswith("## Suggested automations\n\n")
    assert "Accept with `/suggestions accept N` or dismiss with `/suggestions dismiss N`." in text
    assert "- **1. Morning brief** — `08:00` · catalog — A digest of overnight CI failures" in text
    assert "- **2. Weekly review** — `Mon 09:00` · learned" in text
    assert text.rstrip().endswith("__Page 1 of 1 • total 2__")


def test_suggestions_usage_card_lists_subcommands():
    text = _rich_suggestions_text(
        "Usage:\n"
        "  /suggestions              list pending\n"
        "  /suggestions accept N     schedule suggestion N\n"
        "  /suggestions dismiss N    dismiss suggestion N\n"
        "  /suggestions catalog      add curated starter automations\n"
        "  /suggestions clear        housekeeping"
    )
    _assert_card_shape(text)
    assert text.startswith("## Usage\n\n")
    assert "- `/suggestions` — list pending" in text
    assert "- `/suggestions accept N` — schedule suggestion N" in text


def test_suggestions_accepted_card_has_rows():
    text = _rich_suggestions_text(
        "Scheduled 'Morning brief' (08:00). Ask me to list, pause, or remove it any time."
    )
    _assert_card_shape(text)
    assert text.startswith("## Suggestion scheduled\n\n")
    assert "- **Automation:** Morning brief" in text
    assert "- **Schedule:** 08:00" in text


def test_blueprint_catalog_card_rows_and_footer():
    text = _rich_blueprint_text(
        "Automation Blueprints — `/blueprint <name>` and I'll ask you what I need:\n\n"
        "  • morning-brief — Morning brief\n"
        "    A digest of overnight CI failures\n"
        "  • weekly-review — Weekly review (plugin: cron)\n"
        "    Summarise the week's merged PRs\n\n"
        "Tip: `/blueprint <name>` walks you through it. Power users can "
        "pass values inline, e.g. `/blueprint morning-brief time=08:00`."
    )
    _assert_card_shape(text)
    assert text.startswith("## Automation blueprints\n\n")
    assert "- **`morning-brief`** — Morning brief — A digest of overnight CI failures" in text
    assert "- **`weekly-review`** — Weekly review (plugin: cron) — Summarise the week's merged PRs" in text
    assert text.rstrip().endswith("__Page 1 of 1 • total 2__")


def test_blueprint_scheduled_card_has_rows():
    text = _rich_blueprint_text(
        "Scheduled 'Morning brief' (08:00), delivering to origin. "
        "Ask me to list, pause, or remove it any time."
    )
    _assert_card_shape(text)
    assert text.startswith("## Blueprint scheduled\n\n")
    assert "- **Blueprint:** Morning brief" in text
    assert "- **Schedule:** 08:00" in text
    assert "- **Delivering to:** origin" in text


def test_blueprint_no_match_keeps_text_under_heading():
    text = _rich_blueprint_text(
        "No automation blueprint matches 'zzz'. Did you mean: morning-brief? "
        "Run /blueprint to see the catalog."
    )
    _assert_card_shape(text)
    assert text.startswith("## No matching blueprint\n\n")
    assert "No automation blueprint matches 'zzz'." in text


# --- Card delivery through the handlers ---------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_status_card_html_has_sections():
    runner, adapter = _make_runner()
    mgr = MagicMock()
    mgr.status_line.return_value = "\u2665 Heartbeat (every 10m, next in ~300s): check the inbox"
    mgr.session_id = "sess-1"
    runner._get_heartbeat_manager_for_event = AsyncMock(return_value=(mgr, None))
    result = await runner._handle_heartbeat_command(_event("/heartbeat"))
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Status</h2>" in html
    assert "<li><strong>Status:</strong> active</li>" in html
    assert "<li><strong>Prompt:</strong> check the inbox</li>" in html


@pytest.mark.asyncio
async def test_suggestions_failure_card_keeps_error(monkeypatch):
    from hermes_cli import suggestions_cmd

    runner, adapter = _make_runner()
    monkeypatch.setattr(
        suggestions_cmd, "handle_suggestions_command",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("suggestions down")),
    )
    result = await runner._handle_suggestions_command(_event("/suggestions"))
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Suggestions</h2>" in html
    assert "suggestions down" in html


@pytest.mark.asyncio
async def test_pause_card_html_has_reason_row(monkeypatch):
    from agent import estop

    runner, adapter = _make_runner()
    monkeypatch.setattr(estop, "get_state", lambda: None)
    monkeypatch.setattr(estop, "engage", lambda reason=None: None)
    result = await runner._handle_pause_command(_event("/pause deploy freeze"))
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Paused</h2>" in html
    assert "<li><strong>Reason:</strong> deploy freeze</li>" in html


@pytest.mark.asyncio
async def test_blueprint_catalog_result_is_reformatted(monkeypatch):
    from hermes_cli import blueprint_cmd

    runner, _adapter = _make_runner()
    monkeypatch.setattr(
        blueprint_cmd, "handle_blueprint_command",
        lambda *a, **k: blueprint_cmd.BlueprintCommandResult(
            "Automation Blueprints — `/blueprint <name>` and I'll ask you what I need:\n\n"
            "  • morning-brief — Morning brief\n"
            "    A digest of overnight CI failures\n"
        ),
    )
    result = await runner._handle_blueprint_command(_event("/blueprint"))
    assert result.text.startswith("## Automation blueprints\n\n")
    assert "- **`morning-brief`** — Morning brief — A digest of overnight CI failures" in result.text
    assert result.text.rstrip().endswith("__Page 1 of 1 • total 1__")
