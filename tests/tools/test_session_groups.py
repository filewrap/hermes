"""Behaviour contracts for session groups through the ``sessions`` tools
(F2, wave 5): group filter on ``sessions_list``, group patch (move/clear,
single + batch), and the group_list / group_rename / group_delete actions —
directly, through the registry, and through the inline executor (the live
agent path). All run zero LLM calls; every DB is a tmp_path SessionDB
injected via ``db=`` — tests never write to ~/.hermes.
"""
import json
import time
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from hermes_state import SessionDB
from tools.registry import registry
from tools.sessions_tools import sessions, sessions_list


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed(db):
    """Two grouped sessions, one ungrouped, newest first: s_beta, s_alpha, s_plain."""
    now = int(time.time())
    db.create_session("s_alpha", source="cli", group_name="parlay")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 2000, "Alpha notes", "s_alpha"))
    db.create_session("s_beta", source="cli", group_name="hermes")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 1000, "Beta notes", "s_beta"))
    db.create_session("s_plain", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 500, "Plain notes", "s_plain"))
    db._conn.commit()


def _ids(result):
    return [s["session_id"] for s in result["sessions"]]


class TestSessionsListGroupFilter:
    def test_filter_by_group(self, db):
        _seed(db)
        result = json.loads(sessions_list(group="parlay", db=db))
        assert _ids(result) == ["s_alpha"]
        assert result["sessions"][0]["group"] == "parlay"

    def test_filter_ungrouped(self, db):
        _seed(db)
        result = json.loads(sessions_list(group="ungrouped", db=db))
        assert _ids(result) == ["s_plain"]
        assert result["sessions"][0]["group"] is None

    def test_no_filter_lists_all_with_groups(self, db):
        _seed(db)
        result = json.loads(sessions_list(db=db))
        assert _ids(result) == ["s_plain", "s_beta", "s_alpha"]
        assert {s["session_id"]: s["group"] for s in result["sessions"]} == {
            "s_plain": None, "s_beta": "hermes", "s_alpha": "parlay"}


class TestSessionsPatchGroup:
    def test_move_one_session(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_alpha", action="patch", group="music", db=db))
        assert result["success"] is True
        assert result["changed"] == ["group"]
        assert result["session"]["group"] == "music"
        assert _ids(json.loads(sessions_list(group="music", db=db))) == ["s_alpha"]

    def test_clear_group_ungroups(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_alpha", action="patch", group="", db=db))
        assert result["success"] is True
        assert result["session"]["group"] is None
        assert _ids(json.loads(sessions_list(group="ungrouped", db=db))) == ["s_plain", "s_alpha"]

    def test_move_batch_via_targets(self, db):
        _seed(db)
        result = json.loads(sessions(action="patch", group="music",
                                     targets=["s_alpha", "s_beta"], db=db))
        assert result["success"] is True
        assert result["succeeded"] == 2 and result["failed"] == 0
        assert _ids(json.loads(sessions_list(group="music", db=db))) == ["s_beta", "s_alpha"]

    def test_patch_group_unknown_session_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="missing", action="patch", group="music", db=db))
        assert "error" in result

    def test_patch_group_needs_a_field(self, db):
        _seed(db)
        result = json.loads(sessions(session_id="s_alpha", action="patch", db=db))
        assert "error" in result


class TestGroupActions:
    def test_group_list(self, db):
        _seed(db)
        result = json.loads(sessions(action="group_list", db=db))
        assert result["success"] is True
        assert [(g["name"], g["session_count"]) for g in result["groups"]] == [
            ("hermes", 1), ("parlay", 1)]

    def test_group_rename(self, db):
        _seed(db)
        result = json.loads(sessions(action="group_rename", name="parlay", to="music", db=db))
        assert result["success"] is True
        assert result["sessions_updated"] == 1
        assert result["renamed_from"] == "parlay" and result["renamed_to"] == "music"
        assert _ids(json.loads(sessions_list(group="music", db=db))) == ["s_alpha"]

    def test_group_rename_unknown_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(action="group_rename", name="missing", to="music", db=db))
        assert "error" in result

    def test_group_delete(self, db):
        _seed(db)
        result = json.loads(sessions(action="group_delete", name="parlay", db=db))
        assert result["success"] is True
        assert result["sessions_ungrouped"] == 1
        assert _ids(json.loads(sessions_list(group="ungrouped", db=db))) == ["s_plain", "s_alpha"]
        assert [g["name"] for g in
                json.loads(sessions(action="group_list", db=db))["groups"]] == ["hermes"]

    def test_group_delete_unknown_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(action="group_delete", name="missing", db=db))
        assert "error" in result

    def test_unknown_action_is_an_error(self, db):
        _seed(db)
        result = json.loads(sessions(action="group_set", db=db))
        assert "error" in result


class TestRegistrationAndInlinePath:
    def test_schemas_carry_the_group_surface(self):
        list_schema = registry.get_entry("sessions_list").schema
        assert "group" in list_schema["parameters"]["properties"]
        schema = registry.get_entry("sessions").schema
        props = schema["parameters"]["properties"]
        assert "group" in props and "name" in props and "to" in props
        assert set(schema["parameters"]["properties"]["action"]["enum"]) == {
            "get", "patch", "group_list", "group_rename", "group_delete"}

    def test_same_result_via_function_registry_and_inline_executor(self, db):
        """The live path (inline executor with the agent's SessionDB) agrees
        with the function and the registry handler."""
        _seed(db)
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: db,
                                session_id="s_alpha")
        ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")
        direct = json.loads(sessions_list(group="parlay", db=db))
        via_registry = json.loads(registry.get_entry("sessions_list").handler(
            {"group": "parlay"}, db=db))
        via_inline = json.loads(INLINE_TOOL_EXECUTORS["sessions_list"](
            agent, {"group": "parlay"}, ctx))
        assert _ids(direct) == _ids(via_registry) == _ids(via_inline) == ["s_alpha"]

        rename_args = {"action": "group_rename", "name": "parlay", "to": "music"}
        via_registry_rename = json.loads(registry.get_entry("sessions").handler(rename_args, db=db))
        assert via_registry_rename["success"] is True
        assert via_registry_rename["sessions_updated"] == 1
        via_inline_list = json.loads(INLINE_TOOL_EXECUTORS["sessions_list"](
            agent, {"group": "music"}, ctx))
        assert _ids(via_inline_list) == ["s_alpha"]
