"""Behaviour contracts for session groups (F2, wave 5).

Groups live on the ``sessions`` row (``group_name``): a new session is filed
under its project automatically, the agent moves it with
``set_session_group``, and the catalog is derived from live rows. All run
zero LLM calls; every DB is a tmp_path SessionDB injected directly — tests
never write to ~/.hermes.
"""
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _group_of(db, session_id):
    row = db.get_session(session_id)
    assert row is not None
    return row.get("group_name")


class TestAutoGroupOnCreation:
    def test_explicit_group_wins(self, db):
        db.create_session("s1", source="cli", group_name="parlay")
        assert _group_of(db, "s1") == "parlay"

    def test_derived_from_git_repo_root(self, db, tmp_path):
        repo = tmp_path / "parlay"
        repo.mkdir()
        db.create_session("s1", source="cli", git_repo_root=str(repo))
        assert _group_of(db, "s1") == "parlay"

    def test_derived_from_cwd_when_no_repo(self, db, tmp_path):
        project = tmp_path / "hermes-agent"
        project.mkdir()
        db.create_session("s1", source="cli", cwd=str(project))
        assert _group_of(db, "s1") == "hermes-agent"

    def test_repo_root_beats_cwd(self, db, tmp_path):
        repo = tmp_path / "parlay"
        repo.mkdir()
        other = tmp_path / "something-else"
        other.mkdir()
        db.create_session("s1", source="cli", git_repo_root=str(repo), cwd=str(other))
        assert _group_of(db, "s1") == "parlay"

    def test_no_project_stays_ungrouped(self, db):
        db.create_session("s1", source="cli")
        assert _group_of(db, "s1") is None

    def test_home_directory_is_not_a_project(self, db):
        db.create_session("s1", source="cli", cwd=str(Path.home()))
        assert _group_of(db, "s1") is None

    def test_filesystem_root_is_not_a_project(self, db):
        db.create_session("s1", source="cli", cwd="/")
        assert _group_of(db, "s1") is None

    def test_recreate_never_re_groups_a_cleared_session(self, db, tmp_path):
        project = tmp_path / "parlay"
        project.mkdir()
        db.create_session("s1", source="cli", cwd=str(project))
        assert _group_of(db, "s1") == "parlay"
        assert db.set_session_group("s1", "") is True
        assert _group_of(db, "s1") is None
        # A later create/ensure for the same row must not resurrect the group
        # the agent deliberately cleared.
        db.create_session("s1", source="cli", cwd=str(project))
        assert _group_of(db, "s1") is None

    def test_child_inherits_parent_group(self, db):
        db.create_session("parent", source="cli", group_name="parlay")
        db.create_session("child", source="cli", parent_session_id="parent")
        assert _group_of(db, "child") == "parlay"


class TestSetSessionGroup:
    def test_move_and_clear(self, db):
        db.create_session("s1", source="cli", group_name="parlay")
        assert db.set_session_group("s1", "  hermes  ") is True
        assert _group_of(db, "s1") == "hermes"
        assert db.set_session_group("s1", "") is True
        assert _group_of(db, "s1") is None
        assert db.set_session_group("s1", None) is True
        assert _group_of(db, "s1") is None

    def test_unknown_session_is_false(self, db):
        assert db.set_session_group("missing", "parlay") is False

    def test_groups_survive_reopen(self, db, tmp_path):
        db.create_session("s1", source="cli", group_name="parlay")
        db.create_session("s2", source="cli")
        reopened = SessionDB(tmp_path / "state.db")
        assert reopened.get_session("s1").get("group_name") == "parlay"
        assert reopened.get_session("s2").get("group_name") is None
        assert [g["name"] for g in reopened.list_session_groups()] == ["parlay"]


class TestGroupCatalog:
    def _seed(self, db):
        now = int(time.time())
        db.create_session("s1", source="cli", group_name="parlay")
        db.create_session("s2", source="cli", group_name="parlay")
        db.create_session("s3", source="cli", group_name="hermes")
        db.create_session("s4", source="cli")
        for sid, started in (("s1", now - 3000), ("s2", now - 2000),
                             ("s3", now - 1000), ("s4", now - 500)):
            db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                             (started, sid))
        db._conn.commit()

    def test_list_groups_with_counts(self, db):
        self._seed(db)
        groups = db.list_session_groups()
        assert [(g["name"], g["session_count"]) for g in groups] == [
            ("hermes", 1), ("parlay", 2)]
        # Most recently active group first (s3/hermes started last).
        assert groups[0]["name"] == "hermes"

    def test_rename_moves_every_member(self, db):
        self._seed(db)
        assert db.rename_session_group("parlay", "music") == 2
        assert _group_of(db, "s1") == "music"
        assert _group_of(db, "s2") == "music"
        assert [g["name"] for g in db.list_session_groups()] == ["hermes", "music"]

    def test_rename_unknown_raises(self, db):
        self._seed(db)
        with pytest.raises(ValueError, match="unknown session group"):
            db.rename_session_group("missing", "other")

    def test_rename_to_same_name_is_a_noop(self, db):
        self._seed(db)
        assert db.rename_session_group("parlay", "parlay") == 0
        assert _group_of(db, "s1") == "parlay"

    def test_delete_ungroups_every_member(self, db):
        self._seed(db)
        assert db.delete_session_group("parlay") == 2
        assert _group_of(db, "s1") is None
        assert _group_of(db, "s2") is None
        assert [g["name"] for g in db.list_session_groups()] == ["hermes"]

    def test_delete_unknown_raises(self, db):
        self._seed(db)
        with pytest.raises(ValueError, match="unknown session group"):
            db.delete_session_group("missing")


class TestListSessionsRichGroupFilter:
    def _seed(self, db):
        db.create_session("s1", source="cli", group_name="parlay")
        db.create_session("s2", source="cli", group_name="hermes")
        db.create_session("s3", source="cli")

    def test_filter_by_group(self, db):
        self._seed(db)
        rows = db.list_sessions_rich(group="parlay")
        assert [r["id"] for r in rows] == ["s1"]
        assert rows[0]["group_name"] == "parlay"

    def test_filter_ungrouped(self, db):
        self._seed(db)
        rows = db.list_sessions_rich(group="ungrouped")
        assert [r["id"] for r in rows] == ["s3"]

    def test_blank_group_means_no_filter(self, db):
        self._seed(db)
        assert len(db.list_sessions_rich(group="  ")) == 3
        assert len(db.list_sessions_rich(group=None)) == 3
