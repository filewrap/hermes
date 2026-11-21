"""Tests for the F6 session-status tool (thin readout over F2 + the live gauge).

Live-check shape, in the sandbox: the current session reports the live agent's
model id, the gauge's usage percent, and its own label/state — and the model
falls back through the recent persisted route to the session row when no live
agent is present. All run zero LLM calls and zero network fetches. Tests never
write to ~/.hermes: every DB is a tmp_path SessionDB injected via ``db=``.
"""
import json
import time
from types import SimpleNamespace

import pytest

from agent.credits_tracker import CreditsState
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from hermes_state import SessionDB
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
from tools.registry import registry
from tools.session_status_tool import session_status


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db):
    """Two sessions: one mid-turn (last message is the user's), one complete."""
    now = int(time.time())
    db.create_session("s_main", source="cli", model="row-model")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 3000, "Main notes", "s_main"))
    db.append_message("s_main", role="user", content="main first message")
    db.create_session("s_done", source="cli", model="row-model")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 2000, "Done notes", "s_done"))
    db.append_message("s_done", role="assistant", content="done answer")
    db._conn.commit()


def _gauge():
    """Real CreditsState: $5.80 of a $10.00 subscription cap left → 42% used."""
    return CreditsState(subscription_micros=5_800_000, subscription_limit_micros=10_000_000,
                        subscription_usd="5.80", subscription_limit_usd="10.00",
                        remaining_usd="4.20")


_DEFAULT_GAUGE = object()


def _agent(db, session_id="s_main", model="live-model", provider="nous", gauge=_DEFAULT_GAUGE):
    return SimpleNamespace(_session_db=db, session_id=session_id, model=model,
                          provider=provider,
                          _credits_state=_gauge() if gauge is _DEFAULT_GAUGE else gauge)


# =========================================================================
# session_status — the current session
# =========================================================================

class TestSessionStatus:
    def test_current_session_reports_live_model_usage_label_and_state(self, db):
        _seed(db)
        result = json.loads(session_status(db=db, agent=_agent(db),
                                           current_session_id="s_main"))
        assert result["success"] is True
        assert result["model"] == {"model": "live-model", "provider": "nous",
                                   "source": "live-agent"}
        assert result["usage"]["source"] == "live-gauge"
        assert result["usage"]["windows"][0]["used_percent"] == 42.0
        assert result["usage"]["windows"][0]["label"] == "Subscription"
        session = result["session"]
        assert session["available"] is True
        assert session["title"] == "Main notes"
        assert session["state"] == "interrupted"  # last message is the user's
        assert session["running"] is True
        assert session["message_count"] == 1
        assert result["current_session_id"] == "s_main"

    def test_complete_session_state(self, db):
        _seed(db)
        result = json.loads(session_status(session_id="s_done", db=db, agent=_agent(db),
                                           current_session_id="s_main"))
        assert result["session"]["state"] == "complete"
        assert result["session"]["running"] is False
        assert result["session"]["title"] == "Done notes"

    def test_no_target_is_an_error(self, db):
        result = json.loads(session_status(db=db, agent=_agent(db)))
        assert result["success"] is False
        assert "error" in result

    def test_unknown_session_reports_unavailable_block_not_a_failure(self, db):
        _seed(db)
        result = json.loads(session_status(session_id="s_missing", db=db, agent=_agent(db),
                                           current_session_id="s_main"))
        assert result["success"] is True
        assert result["session"]["available"] is False
        assert "not found" in result["session"]["error"]
        # The model half still answers: the unknown id only voids the session block.
        assert result["model"]["model"] == "live-model"

    def test_no_gauge_reports_unavailable_usage(self, db):
        _seed(db)
        result = json.loads(session_status(db=db, agent=_agent(db, gauge=None),
                                           current_session_id="s_main"))
        assert result["usage"]["windows"] == []
        assert result["usage"]["unavailable_reason"]


# =========================================================================
# model fallback chain: live agent → recent route → session row
# =========================================================================

class TestModelFallback:
    def test_recent_route_wins_without_a_live_agent(self, db):
        _seed(db)
        with db._lock:
            db._conn.execute(
                "INSERT INTO session_model_usage (session_id, model, billing_provider, task,"
                " api_call_count, last_seen) VALUES ('s_main', ?, 'route-provider', '', 1, 100.0)",
                ("route-model",),
            )
            db._conn.commit()
        agent = _agent(db, model="", provider="")
        result = json.loads(session_status(db=db, agent=agent, current_session_id="s_main"))
        assert result["model"] == {"model": "route-model", "provider": "route-provider",
                                   "source": "recent-route"}

    def test_row_model_is_the_last_fallback(self, db):
        _seed(db)
        agent = _agent(db, model="", provider="")
        result = json.loads(session_status(db=db, agent=agent, current_session_id="s_main"))
        assert result["model"] == {"model": "row-model", "provider": None,
                                   "source": "session-row"}

    def test_no_agent_no_row_model_is_unknown(self, db):
        _seed(db)
        db._conn.execute("UPDATE sessions SET model = '' WHERE id = 's_main'")
        db._conn.commit()
        result = json.loads(session_status(db=db, agent=None, current_session_id="s_main"))
        assert result["model"] == {"model": None, "provider": None, "source": "unknown"}
        assert result["usage"]["windows"] == []


# =========================================================================
# registration: toolset, core tools, inline executor
# =========================================================================

class TestSessionStatusRegistration:
    def test_registered_in_the_sessions_toolset_and_core_tools(self):
        assert registry.get_entry("session_status") is not None
        assert "session_status" in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["sessions"]["tools"]) == {
            "sessions_list", "sessions", "agents_list", "session_status"}

    def test_same_result_via_function_registry_and_inline_executor(self, db):
        _seed(db)
        agent = _agent(db)
        ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")
        direct = json.loads(session_status(db=db, agent=agent, current_session_id="s_main"))
        via_registry = json.loads(registry.get_entry("session_status").handler(
            {}, db=db, agent=agent, current_session_id="s_main"))
        via_inline = json.loads(INLINE_TOOL_EXECUTORS["session_status"](
            agent, {}, ctx))
        assert direct["model"] == via_registry["model"] == via_inline["model"]
        assert direct["usage"] == via_registry["usage"] == via_inline["usage"]
        assert direct["session"]["title"] == via_registry["session"]["title"] \
            == via_inline["session"]["title"] == "Main notes"

    def test_inline_executor_is_registered(self):
        assert "session_status" in INLINE_TOOL_EXECUTORS
