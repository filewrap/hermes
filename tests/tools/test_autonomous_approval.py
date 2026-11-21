"""Autonomous approval bypass: the session set, the terminal gate, the
``suggest_task`` seam, and the delegated-child callback.

Contracts: autonomous bypasses approvals without touching the user's /yolo
state; a dangerous command that would prompt is auto-approved and the human
callback is never consulted; hardline blocks still hold; ``suggest_task``
auto-runs only when the turn itself is autonomous and never on a tainted
turn; a child spawned by an autonomous turn auto-approves, unless the
parent turn is tainted.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from agent.autonomous_context import set_autonomous_turn
from agent.turn_taint import get_turn_taint_state
from tools import approval
from tools.approval_context import reset_current_session_key, set_current_session_key
from tools import suggest_task_tool
from tools.delegate_tool_config import (
    _get_subagent_approval_callback,
    _subagent_auto_approve,
    _subagent_auto_deny,
)


@pytest.fixture
def autonomous_off(monkeypatch):
    """A clean autonomous slate: frozen --yolo off, contextvar cleared."""
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    set_autonomous_turn(False)
    yield
    set_autonomous_turn(False)


@pytest.fixture
def session_key():
    key = f"auto-{uuid.uuid4().hex[:8]}"
    yield key
    approval.clear_session(key)


@pytest.fixture
def bound_key(session_key):
    """The approval session key bound for the current context."""
    token = set_current_session_key(session_key)
    yield session_key
    reset_current_session_key(token)


def _agent(spawn=None):
    agent = SimpleNamespace(session_id="s-auto", _interrupt_requested=False)
    if spawn is not None:
        agent._dispatch_delegate_task = spawn
    return agent


def _spawn_ok(calls):
    def _spawn(args):
        calls.append(args)
        return json.dumps({"success": True, "results": [
            {"status": "completed", "subagent_id": "run-auto",
             "summary": "follow-up done"}]})
    return _spawn


def test_autonomous_bypass_is_separate_from_yolo(autonomous_off, bound_key):
    """Enabling autonomous bypasses approvals and leaves /yolo untouched."""
    before = approval.is_approval_bypass_active_for_session(bound_key)
    approval.enable_session_autonomous(bound_key)
    try:
        assert approval.is_session_autonomous_enabled(bound_key) is True
        assert approval.is_session_yolo_enabled(bound_key) is False
        assert approval.is_approval_bypass_active_for_session(bound_key) is True
        assert approval.is_approval_bypass_active() is True
    finally:
        approval.disable_session_autonomous(bound_key)
    assert approval.is_session_autonomous_enabled(bound_key) is False
    assert approval.is_approval_bypass_active_for_session(bound_key) is before


def test_clear_session_drops_autonomous(autonomous_off, session_key):
    approval.enable_session_autonomous(session_key)
    assert approval.is_session_autonomous_enabled(session_key) is True
    approval.clear_session(session_key)
    assert approval.is_session_autonomous_enabled(session_key) is False


def test_terminal_gate_skips_prompt_when_autonomous(
        autonomous_off, bound_key, monkeypatch):
    """A dangerous command that would prompt is approved without asking.

    With a human present (``HERMES_INTERACTIVE``) and a deny callback, the
    gate denies when autonomous is off and consults the callback; with
    autonomous on the same command is approved and the callback is never
    consulted again.
    """
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    calls = []

    def deny_cb(command, description=None, **kwargs):
        calls.append(command)
        return "deny"

    denied = approval.check_dangerous_command(
        "rm -rf /tmp/x", "local", approval_callback=deny_cb)
    assert denied["approved"] is False
    assert len(calls) == 1

    approval.enable_session_autonomous(bound_key)
    try:
        allowed = approval.check_dangerous_command(
            "rm -rf /tmp/x", "local", approval_callback=deny_cb)
    finally:
        approval.disable_session_autonomous(bound_key)
    assert allowed["approved"] is True
    assert len(calls) == 1


def test_hardline_still_blocks_under_autonomous(autonomous_off, bound_key):
    """The unconditional blocklist is not bypassed by autonomous mode."""
    approval.enable_session_autonomous(bound_key)
    try:
        result = approval.check_dangerous_command("rm -rf /", "local")
    finally:
        approval.disable_session_autonomous(bound_key)
    assert result["approved"] is False
    assert result.get("hardline") is True


def test_suggest_task_auto_runs_only_when_turn_is_autonomous(autonomous_off):
    """The seam answers from the per-turn flag, not from a global toggle."""
    calls = []
    agent = _agent(spawn=_spawn_ok(calls))

    staged = json.loads(suggest_task_tool.suggest_task(
        title="Tidy the downloads folder",
        prompt="Organize ~/Downloads by kind.", parent_agent=agent))
    assert staged["status"] == "pending"
    assert staged["approval"] is None
    assert calls == []

    set_autonomous_turn(True)
    try:
        started = json.loads(suggest_task_tool.suggest_task(
            title="Tidy the downloads folder",
            prompt="Organize ~/Downloads by kind.", parent_agent=agent))
    finally:
        set_autonomous_turn(False)
    assert started["status"] == "started"
    assert started["approval"] == "autonomous"
    assert len(calls) == 1


def test_suggest_task_tainted_turn_stays_pending(autonomous_off):
    """A tainted autonomous turn never auto-approves; the owner decides."""
    calls = []
    agent = _agent(spawn=_spawn_ok(calls))
    get_turn_taint_state(agent).observe("network")

    set_autonomous_turn(True)
    try:
        result = json.loads(suggest_task_tool.suggest_task(
            title="Tidy the downloads folder",
            prompt="Organize ~/Downloads by kind.", parent_agent=agent))
    finally:
        set_autonomous_turn(False)
    assert result["status"] == "pending"
    assert result["approval"] is None
    assert calls == []


def test_delegated_child_auto_approves_on_autonomous_turn(autonomous_off):
    """A child spawned by an autonomous turn gets the auto-approve callback."""
    set_autonomous_turn(True)
    try:
        callback = _get_subagent_approval_callback(_agent())
    finally:
        set_autonomous_turn(False)
    assert callback is _subagent_auto_approve
    assert callback("rm -rf /tmp/x", "delete in root path") == "once"


def test_delegated_child_taint_wins_over_autonomous(autonomous_off):
    """A tainted parent denies even when the turn is autonomous."""
    agent = _agent()
    get_turn_taint_state(agent).observe("network")
    set_autonomous_turn(True)
    try:
        callback = _get_subagent_approval_callback(agent)
    finally:
        set_autonomous_turn(False)
    assert callback is _subagent_auto_deny
    assert callback("rm -rf /tmp/x", "delete in root path") == "deny"


def test_delegated_child_denies_by_default(autonomous_off):
    """Outside autonomous mode (and without the config opt-in) children deny."""
    callback = _get_subagent_approval_callback(_agent())
    assert callback is _subagent_auto_deny
    assert callback("rm -rf /tmp/x", "delete in root path") == "deny"
