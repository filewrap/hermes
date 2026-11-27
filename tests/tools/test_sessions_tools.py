"""Tests for the F2 session-registry tools (thin wrappers over SessionDB).

Live-check shape, in the sandbox: list sessions with labels, pin one, archive
another, and a later sessions_list call reflects the change. All run zero
LLM calls. Tests never write to ~/.hermes: every DB is a tmp_path SessionDB
injected via ``db=``.
"""
import json
import time
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from hermes_state import SessionDB
from tools.registry import registry
# Imported for its registration side effect: the F6 tool registers on import.
import tools.session_status_tool
from tools.sessions_tools import agents_list, sessions, sessions_list


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db):
    """Three sessions: one plain, one hidden, one with messages."""
    now = int(time.time())
    db.create_session("s_alpha", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 3000, "Alpha notes", "s_alpha"))
    db.append_message("s_alpha", role="user", content="alpha first message")
    db.create_session("s_beta", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 2000, "Beta notes", "s_beta"))
    db.append_message("s_beta", role="user", content="beta first message")
    db.create_session("s_hidden", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ?, hidden = 1 WHERE id = ?",
                     (now - 1000, "Hidden notes", "s_hidden"))
    db._conn.commit()


def _ids(result):
    return [s["session_id"] for s in result["sessions"]]


# =========================================================================
# sessions_list
# =========================================================================

class TestSessionsList:
    def test_lists_newest_first_with_labels_and_preview(self, db):
        _seed(db)
        result = json.loads(sessions_list(db=db))
        assert result["success"] is True
        assert _ids(result) == ["s_beta", "s_alpha"]  # hidden row stays out
        beta = result["sessions"][0]
        assert beta["title"] == "Beta notes"
        assert beta["source"] == "cli"
        assert beta["pinned"] is False and beta["archived"] is False
        assert beta["message_count"] == 1
        assert "beta first message" in (beta["preview"] or "")
        assert beta["link"].endswith("/s_beta")
        assert result["count"] == 2 and result["has_more"] is False

    def test_limit_and_offset_page(self, db):
        _seed(db)
        first = json.loads(sessions_list(limit=1, db=db))
        assert _ids(first) == ["s_beta"] and first["has_more"] is True
        second = json.loads(sessions_list(limit=1, offset=1, db=db))
        assert _ids(second) == ["s_alpha"] and second["has_more"] is False

    def test_search_matches_title(self, db):
        _seed(db)
        result = json.loads(sessions_list(search="Alpha", db=db))
        assert _ids(result) == ["s_alpha"]

    def test_archived_views(self, db):
        _seed(db)
        db.set_session_archived("s_alpha", True)
        assert _ids(json.loads(sessions_list(db=db))) == ["s_beta"]
        assert _ids(json.loads(sessions_list(archived="archived", db=db))) == ["s_alpha"]
        assert _ids(json.loads(sessions_list(archived="all", db=db))) == ["s_beta", "s_alpha"]

    def test_pinned_only(self, db):
        _seed(db)
        db.set_session_pinned("s_alpha", True)
        assert _ids(json.loads(sessions_list(pinned_only=True, db=db))) == ["s_alpha"]
        # A pinned row stamped archived stays visible (a pin is an explicit
        # keep), in both the default view and the pinned-only view.
        db.set_session_archived("s_alpha", True)
        assert _ids(json.loads(sessions_list(db=db))) == ["s_beta", "s_alpha"]
        assert _ids(json.loads(sessions_list(pinned_only=True, db=db))) == ["s_alpha"]
        assert _ids(json.loads(sessions_list(archived="archived", db=db))) == ["s_alpha"]

    def test_bad_archived_value_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions_list(archived="nope", db=db))
        assert "error" in result


# =========================================================================
# sessions — get / patch
# =========================================================================

class TestSessionsGet:
    def test_get_returns_projected_row(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_alpha", db=db))
        assert result["success"] is True
        assert result["session"]["title"] == "Alpha notes"
        assert result["session"]["session_id"] == "s_alpha"

    def test_get_defaults_to_current_session(self, db):
        _seed(db)
        result = json.loads(sessions(db=db, current_session_id="s_beta"))
        assert result["session"]["session_id"] == "s_beta"

    def test_get_unknown_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_missing", db=db))
        assert "error" in result and "not found" in result["error"]


class TestSessionsPatch:
    def test_label_pin_archive_then_list_reflects_the_change(self, db):
        """Plan live check, sandbox form: pin one, archive another, re-list."""
        _seed(db)
        pinned = json.loads(sessions(session_id="s_alpha", action="patch",
                                     pinned=True, db=db))
        assert pinned["success"] is True
        assert pinned["changed"] == ["pinned"]
        assert pinned["session"]["pinned"] is True
        archived = json.loads(sessions(session_id="s_beta", action="patch",
                                       archived=True, db=db))
        assert archived["success"] is True
        assert archived["session"]["archived"] is True
        listed = json.loads(sessions_list(db=db))
        assert _ids(listed) == ["s_alpha"]  # pinned stays, archived drops out
        assert listed["sessions"][0]["pinned"] is True
        assert _ids(json.loads(sessions_list(archived="archived", db=db))) == ["s_beta"]

    def test_label_sets_user_title_and_empty_clears_it(self, db):
        _seed(db)
        renamed = json.loads(sessions(session_id="s_alpha", action="patch",
                                      label="Renamed alpha", db=db))
        assert renamed["success"] is True
        assert renamed["session"]["title"] == "Renamed alpha"
        assert db.get_session("s_alpha")["title_source"] == "user"
        cleared = json.loads(sessions(session_id="s_alpha", action="patch",
                                      label="", db=db))
        assert cleared["success"] is True
        assert cleared["session"]["title"] is None

    def test_label_over_100_chars_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_alpha", action="patch",
                                     label="x" * 101, db=db))
        assert "error" in result

    def test_pin_unhides_a_hidden_session(self, db):
        _seed(db)
        assert _ids(json.loads(sessions_list(db=db))) == ["s_beta", "s_alpha"]
        json.loads(sessions(session_id="s_hidden", action="patch", pinned=True, db=db))
        assert "s_hidden" in _ids(json.loads(sessions_list(db=db)))

    def test_pin_survives_reopen(self, db, tmp_path):
        """A pin is a durable row, not process state: close the DB, reopen
        it, and the pinned session is still listed as pinned."""
        _seed(db)
        json.loads(sessions(session_id="s_alpha", action="patch", pinned=True, db=db))
        db.close()
        reopened = SessionDB(tmp_path / "state.db")
        try:
            listed = json.loads(sessions_list(db=reopened))
            assert _ids(listed) == ["s_beta", "s_alpha"]
            assert listed["sessions"][1]["pinned"] is True
        finally:
            reopened.close()

    def test_archive_then_restore(self, db):
        _seed(db)
        json.loads(sessions(session_id="s_alpha", action="patch", archived=True, db=db))
        assert _ids(json.loads(sessions_list(db=db))) == ["s_beta"]
        json.loads(sessions(session_id="s_alpha", action="patch", archived=False, db=db))
        assert _ids(json.loads(sessions_list(db=db))) == ["s_beta", "s_alpha"]

    def test_batch_patch_and_caps(self, db):
        _seed(db)
        batch = json.loads(sessions(action="patch", targets=["s_alpha", "s_beta"],
                                    pinned=True, db=db))
        assert batch["success"] is True
        assert batch["succeeded"] == 2 and batch["failed"] == 0
        assert _ids(json.loads(sessions_list(pinned_only=True, db=db))) == ["s_beta", "s_alpha"]
        over = json.loads(sessions(action="patch", targets=[f"s_{i}" for i in range(101)],
                                   pinned=True, db=db))
        assert "error" in over
        mixed = json.loads(sessions(session_id="s_alpha", action="patch",
                                    targets=["s_beta"], pinned=True, db=db))
        assert "error" in mixed

    def test_patch_with_no_fields_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_alpha", action="patch", db=db))
        assert "error" in result

    def test_patch_unknown_session_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_missing", action="patch",
                                     pinned=True, db=db))
        assert "error" in result


# =========================================================================
# agents_list
# =========================================================================

class TestAgentsList:
    def test_current_agent_entry(self, db):
        _seed(db)
        db._conn.execute("UPDATE sessions SET model = ? WHERE id = ?",
                         ("test-model", "s_alpha"))
        db._conn.commit()
        result = json.loads(agents_list(db=db, current_session_id="s_alpha"))
        assert result["success"] is True
        assert result["count"] == 1
        main = result["agents"][0]
        assert main["id"] == "s_alpha" and main["kind"] == "main"
        assert main["model"] == "test-model" and main["status"] == "active"

    def test_live_subagents_are_listed(self, db, monkeypatch):
        monkeypatch.setattr(
            "tools.delegate_tool_registry.list_active_subagents",
            lambda: [{"subagent_id": "child-1", "parent_id": "s_alpha", "depth": 1,
                      "goal": "summarise the logs", "model": "test-model",
                      "started_at": 1700000000, "status": "running"}],
        )
        result = json.loads(agents_list(db=db, current_session_id="s_alpha"))
        assert result["count"] == 2
        child = result["agents"][1]
        assert child["id"] == "child-1" and child["kind"] == "subagent"
        assert child["name"] == "summarise the logs"


# =========================================================================
# Registration + live-path wiring
# =========================================================================

class TestRegistrationAndInlinePath:
    def test_all_four_tools_registered_in_the_sessions_toolset(self):
        from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

        for name in ("sessions_list", "sessions", "agents_list", "session_status"):
            assert registry.get_entry(name) is not None
            assert name in _HERMES_CORE_TOOLS
        assert set(TOOLSETS["sessions"]["tools"]) == {
            "sessions_list", "sessions", "agents_list", "session_status"}

    def test_same_result_via_function_registry_and_inline_executor(self, db):
        """The live path (inline executor with the agent's SessionDB) agrees
        with the function and the registry handler."""
        _seed(db)
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: db,
                                session_id="s_alpha")
        ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")
        direct = json.loads(sessions_list(db=db))
        via_registry = json.loads(registry.get_entry("sessions_list").handler({}, db=db))
        via_inline = json.loads(INLINE_TOOL_EXECUTORS["sessions_list"](agent, {}, ctx))
        assert _ids(direct) == _ids(via_registry) == _ids(via_inline) == ["s_beta", "s_alpha"]

        patch_args = {"session_id": "s_beta", "action": "patch", "archived": True}
        json.loads(sessions(db=db, **patch_args))
        assert _ids(json.loads(sessions_list(db=db))) == ["s_alpha"]
        via_inline_patch = json.loads(
            INLINE_TOOL_EXECUTORS["sessions"](agent, patch_args, ctx))
        assert "error" not in via_inline_patch  # already-archived re-patch is idempotent
        assert _ids(json.loads(
            INLINE_TOOL_EXECUTORS["sessions_list"](agent, {}, ctx))) == ["s_alpha"]

    def test_inline_executors_are_registered(self):
        for name in ("sessions_list", "sessions", "agents_list"):
            assert name in INLINE_TOOL_EXECUTORS


# =========================================================================
# sessions delete (owner-only, confirm step) — ruling 2026-11-17
# =========================================================================

_OWNER_ID = "6802929470"
_NON_OWNER_ID = "123456789"


def _set_launch_owner(monkeypatch):
    """Write OWNER_TELEGRAM_ID into the launch profile's .env under the test home.

    The "default" profile's home IS the HERMES_HOME root (``get_profile_dir("default")``
    returns it), so the .env lives there, not under ``profiles/default/``.
    """
    from hermes_constants import get_hermes_home

    (get_hermes_home() / ".env").write_text(
        f"OWNER_TELEGRAM_ID={_OWNER_ID}\n", encoding="utf-8")


class TestSessionsDelete:
    def test_delete_without_confirm_reports_and_keeps_session(self, db):
        _seed(db)
        result = json.loads(sessions(action="delete", session_id="s_alpha", db=db))
        assert result["success"] is False
        assert "confirm" in result["error"].lower()
        assert db.get_session("s_alpha") is not None

    def test_delete_with_confirm_removes_session(self, db):
        _seed(db)
        result = json.loads(
            registry.get_entry("sessions").handler(
                {"session_id": "s_alpha", "action": "delete", "confirm": True}, db=db))
        assert result["success"] is True
        assert result["deleted"] == "s_alpha"
        assert db.get_session("s_alpha") is None
        assert _ids(json.loads(sessions_list(db=db))) == ["s_beta"]

    def test_delete_missing_session_fails_plainly(self, db):
        _seed(db)
        result = json.loads(
            sessions(action="delete", session_id="no-such", confirm=True, db=db))
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_delete_non_owner_denied(self, db, monkeypatch):
        _seed(db)
        _set_launch_owner(monkeypatch)
        from gateway.session_context import clear_session_vars, set_session_vars

        tokens = set_session_vars(actor_id=_NON_OWNER_ID)
        try:
            result = json.loads(
                sessions(action="delete", session_id="s_alpha", confirm=True, db=db))
        finally:
            clear_session_vars(tokens)
        assert result["success"] is False
        assert "owner only" in result["error"].lower()
        assert db.get_session("s_alpha") is not None

    def test_delete_owner_actor_allowed(self, db, monkeypatch):
        _seed(db)
        _set_launch_owner(monkeypatch)
        from gateway.session_context import clear_session_vars, set_session_vars

        tokens = set_session_vars(actor_id=_OWNER_ID)
        try:
            result = json.loads(
                sessions(action="delete", session_id="s_alpha", confirm=True, db=db))
        finally:
            clear_session_vars(tokens)
        assert result["success"] is True
        assert db.get_session("s_alpha") is None

    def test_delete_busy_session_refused_plainly(self, db):
        import hermes_state_runtime as rt

        db.create_session("s1", source="cli")
        epoch = rt.begin_runtime_epoch(db, instance_id="a")
        rt.admit_session_input(db, epoch=epoch, principal_id="p",
                               session_id="s1", request_id="r", payload={"text": "x"})
        result = json.loads(
            sessions(action="delete", session_id="s1", confirm=True, db=db))
        assert result["success"] is False
        assert "turn" in result["error"].lower()
        assert db.get_session("s1") is not None

    def test_delete_via_inline_executor_passes_confirm(self, db):
        _seed(db)
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: db,
                                session_id="s_alpha")
        ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")
        result = json.loads(INLINE_TOOL_EXECUTORS["sessions"](
            agent, {"session_id": "s_beta", "action": "delete", "confirm": True}, ctx))
        assert result["success"] is True
        assert db.get_session("s_beta") is None
