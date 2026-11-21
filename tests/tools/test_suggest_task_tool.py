"""F7 suggest_task / dismiss_task: validation, staging, owner-card approval,
dismiss-withdraw, and the autonomous auto-run seam.

Live-check shape, in the sandbox: the approval card is the real gateway
wait (``_await_gateway_decision``) with a stub notify callback standing in
for the runner's ``_approval_notify_sync``; a resolve from another thread
stands in for the owner's tap / ``/approve``. The follow-up spawn is the
agent's own ``_dispatch_delegate_task`` seam, stubbed on a fake parent
agent. Zero LLM calls; no live gateway, no live Telegram connection.
"""

import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tests.waiting import wait_until
from tools import approval as approval_mod
from tools import approval_context
from tools import suggest_task_tool as tools_mod


@pytest.fixture
def session_key():
    return f"s-f7-{uuid.uuid4().hex[:8]}"


def _agent(session_id, spawn=None):
    agent = SimpleNamespace(
        session_id=session_id, _interrupt_requested=False,
        _delegate_depth=0, _touch_activity=lambda _desc: None,
    )
    if spawn is not None:
        agent._dispatch_delegate_task = spawn
    return agent


def _ctx():
    return InlineToolContext(effective_task_id="t-1", tool_call_id="call-1")


def _call(name, agent, args):
    return json.loads(INLINE_TOOL_EXECUTORS[name](agent, args, _ctx()))


def _spawn_ok(calls):
    def _spawn(args):
        calls.append(args)
        return json.dumps({"success": True, "results": [
            {"status": "completed", "subagent_id": "run-f7",
             "summary": "follow-up done"}]})
    return _spawn


def _suggested(session_key, agent, **over):
    args = {"title": "Tidy the downloads folder", "prompt": "Organize ~/Downloads by kind."}
    args.update(over)
    return _call("suggest_task", agent, args)


class TestValidation:
    def test_empty_title_rejected(self, session_key):
        result = _suggested(session_key, _agent("s-1"), title="  ")
        assert result["success"] is False

    def test_title_over_60_rejected(self, session_key):
        result = _suggested(session_key, _agent("s-1"), title="x" * 61)
        assert result["success"] is False

    def test_title_at_60_accepted(self, session_key):
        result = _suggested(session_key, _agent("s-1"), title="x" * 60)
        assert result["success"] is True
        assert result["status"] == "pending"

    def test_empty_prompt_rejected(self, session_key):
        result = _suggested(session_key, _agent("s-1"), prompt=" ")
        assert result["success"] is False

    def test_prompt_over_32k_rejected(self, session_key):
        result = _suggested(session_key, _agent("s-1"), prompt="x" * (32 * 1024 + 1))
        assert result["success"] is False

    def test_non_string_tldr_rejected(self, session_key):
        result = _suggested(session_key, _agent("s-1"), tldr=42)
        assert result["success"] is False


class TestStageOnly:
    def test_no_notify_stages_pending_and_persists(self, session_key):
        agent = _agent("s-1")
        result = _suggested(session_key, agent)
        assert result["success"] is True
        assert result["status"] == "pending"
        assert result["task_id"].startswith("sug_")
        record = tools_mod._load_record(result["task_id"])
        assert record is not None
        assert record["title"] == "Tidy the downloads folder"
        assert record["status"] == "pending"

    def test_dismiss_removes_pending(self, session_key):
        result = _suggested(session_key, _agent("s-1"))
        dismissed = _call("dismiss_task", _agent("s-1"), {"task_id": result["task_id"]})
        assert dismissed["success"] is True
        assert dismissed["status"] == "dismissed"
        assert tools_mod._load_record(result["task_id"]) is None

    def test_dismiss_unknown_id_errors(self, session_key):
        result = _call("dismiss_task", _agent("s-1"), {"task_id": "sug_nope"})
        assert result["success"] is False

    def test_dismiss_non_pending_errors(self, session_key):
        staged = _suggested(session_key, _agent("s-1"))
        record = tools_mod._load_record(staged["task_id"])
        record["status"] = "denied"
        tools_mod._save_record(record)
        result = _call("dismiss_task", _agent("s-1"), {"task_id": staged["task_id"]})
        assert result["success"] is False
        assert "denied" in result["error"]


class TestApprovalWait:
    @pytest.fixture
    def notify(self, session_key):
        """A stub notify callback standing in for the runner's card send."""
        seen: dict = {}
        approval_mod.register_gateway_notify(
            session_key, lambda data: seen.setdefault("data", data))
        try:
            yield seen
        finally:
            approval_mod.unregister_gateway_notify(session_key)

    def _suggest_in_thread(self, session_key, agent, out):
        token = approval_context.set_current_session_key(session_key)
        try:
            out["result"] = _suggested(session_key, agent)
        finally:
            approval_context.reset_current_session_key(token)

    def test_approve_once_starts_follow_up(self, session_key, notify):
        calls: list = []
        agent = _agent("s-1", spawn=_spawn_ok(calls))
        out: dict = {}
        thread = threading.Thread(
            target=self._suggest_in_thread, args=(session_key, agent, out), daemon=True)
        thread.start()
        assert wait_until(lambda: "data" in notify, timeout=5)
        data = notify["data"]
        # The card offers Approve-once / Deny only — never a standing grant.
        assert data["allow_permanent"] is False
        assert data["allow_session"] is False
        assert data["command"] == "Tidy the downloads folder"
        assert data["pattern_key"].startswith("suggest_task:sug_")
        resolved = approval_mod.resolve_gateway_approval(
            session_key, "once", request_id=data["request_id"])
        assert resolved == 1
        thread.join(timeout=10)
        assert out["result"]["success"] is True
        assert out["result"]["status"] == "started"
        assert out["result"]["approval"] == "once"
        assert len(calls) == 1
        assert calls[0]["goal"] == "Organize ~/Downloads by kind."
        record = tools_mod._load_record(out["result"]["task_id"])
        assert record["status"] == "started"
        assert record["follow_up"] is not None

    def test_deny_marks_denied_and_starts_nothing(self, session_key, notify):
        calls: list = []
        agent = _agent("s-1", spawn=_spawn_ok(calls))
        out: dict = {}
        thread = threading.Thread(
            target=self._suggest_in_thread, args=(session_key, agent, out), daemon=True)
        thread.start()
        assert wait_until(lambda: "data" in notify, timeout=5)
        approval_mod.resolve_gateway_approval(
            session_key, "deny", reason="not now",
            request_id=notify["data"]["request_id"])
        thread.join(timeout=10)
        assert out["result"]["status"] == "denied"
        assert calls == []
        record = tools_mod._load_record(out["result"]["task_id"])
        assert record["status"] == "denied"
        assert record["deny_reason"] == "not now"

    def test_timeout_leaves_pending(self, session_key, notify, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 1)
        out: dict = {}
        thread = threading.Thread(
            target=self._suggest_in_thread,
            args=(session_key, _agent("s-1"), out), daemon=True)
        thread.start()
        assert wait_until(lambda: "data" in notify, timeout=5)
        thread.join(timeout=10)
        assert out["result"]["status"] == "pending"
        assert "approval window" in out["result"]["message"]
        record = tools_mod._load_record(out["result"]["task_id"])
        assert record["status"] == "pending"

    def test_dismiss_while_waiting_withdraws_and_starts_nothing(
            self, session_key, notify):
        calls: list = []
        agent = _agent("s-1", spawn=_spawn_ok(calls))
        out: dict = {}
        thread = threading.Thread(
            target=self._suggest_in_thread, args=(session_key, agent, out), daemon=True)
        thread.start()
        assert wait_until(lambda: "data" in notify, timeout=5)
        task_id = notify["data"]["pattern_key"].removeprefix("suggest_task:")
        dismissed = _call("dismiss_task", _agent("s-1"), {"task_id": task_id})
        assert dismissed["success"] is True
        thread.join(timeout=10)
        # A withdrawal is not a user deny: still pending in the tool result,
        # record gone, no follow-up started.
        assert out["result"]["status"] == "pending"
        assert "withdrawn" in out["result"]["message"]
        assert calls == []
        assert tools_mod._load_record(task_id) is None

    def test_spawn_failure_reports_approved_but_unstarted(
            self, session_key, notify):
        def _refuse(args):
            return json.dumps({"success": False, "error": "Delegation spawning is paused."})

        agent = _agent("s-1", spawn=_refuse)
        out: dict = {}
        thread = threading.Thread(
            target=self._suggest_in_thread, args=(session_key, agent, out), daemon=True)
        thread.start()
        assert wait_until(lambda: "data" in notify, timeout=5)
        approval_mod.resolve_gateway_approval(
            session_key, "once", request_id=notify["data"]["request_id"])
        thread.join(timeout=10)
        assert out["result"]["status"] == "approved"
        assert "Delegation spawning is paused" in out["result"]["message"]
        record = tools_mod._load_record(out["result"]["task_id"])
        assert record["status"] == "approved"
        assert record["spawn_error"]


class TestAutonomousSeam:
    def test_autonomous_runs_with_no_card_and_no_approval(self, session_key, monkeypatch):
        # The seam: autonomous mode has no representation in code yet, so the
        # test drives it through the single named check the parent wires.
        monkeypatch.setattr(tools_mod, "_autonomous_mode_enabled", lambda: True)
        calls: list = []
        agent = _agent("s-1", spawn=_spawn_ok(calls))
        result = _suggested(session_key, agent)
        assert result["success"] is True
        assert result["status"] == "started"
        assert result["approval"] == "autonomous"
        assert len(calls) == 1
        assert calls[0]["goal"] == "Organize ~/Downloads by kind."
        record = tools_mod._load_record(result["task_id"])
        assert record["approval"] == "autonomous"

    def test_autonomous_check_is_off_by_default(self):
        assert tools_mod._autonomous_mode_enabled() is False


class TestRegistration:
    def test_tools_registered_in_conversations_toolset(self):
        from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

        assert "suggest_task" in _HERMES_CORE_TOOLS
        assert "dismiss_task" in _HERMES_CORE_TOOLS
        assert "suggest_task" in TOOLSETS["conversations"]["tools"]
        assert "dismiss_task" in TOOLSETS["conversations"]["tools"]
        assert "suggest_task" in INLINE_TOOL_EXECUTORS
        assert "dismiss_task" in INLINE_TOOL_EXECUTORS
