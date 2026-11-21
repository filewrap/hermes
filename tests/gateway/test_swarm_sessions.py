"""Swarm session store: durable per-swarm records mirroring deep-research sessions.

Behaviour contracts only: create/load/update/list, owner binding, allowlisted
updates, and the live snapshot that merges durable records with the live
delegation/subagent registries.
"""

import threading
import time

import pytest

from gateway import swarm_sessions as ss


def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_create_and_load_round_trip(tmp_path):
    home = _home(tmp_path)
    record = ss.create_session(
        home, "12345", kind="kanban", source_id="t_abcdef1234",
        goal="Map the market", chat_id="42", chat_type="dm",
        thread_id="777", board="default",
        workers=[{"label": "Market scan", "status": "ready", "task_id": "t_aaa"}],
    )
    assert len(record["session_id"]) == 12
    assert all(c in "0123456789abcdef" for c in record["session_id"])
    assert record["status"] == "starting"
    assert record["kind"] == "kanban"
    assert record["goal"] == "Map the market"
    assert record["workers"][0]["label"] == "Market scan"
    loaded = ss.load_session(home, "12345", record["session_id"])
    assert loaded == record


def test_create_rejects_unknown_kind_and_empty_goal(tmp_path):
    home = _home(tmp_path)
    with pytest.raises(ValueError):
        ss.create_session(home, "12345", kind="teleport", source_id="x",
                          goal="g", chat_id="42", chat_type="dm")
    with pytest.raises(ValueError):
        ss.create_session(home, "12345", kind="delegation", source_id="x",
                          goal="   ", chat_id="42", chat_type="dm")


def test_update_rejects_unknown_fields_and_bad_status(tmp_path):
    home = _home(tmp_path)
    record = ss.create_session(home, "12345", kind="delegation", source_id="d1",
                               goal="g", chat_id="42", chat_type="dm")
    with pytest.raises(ValueError):
        ss.update_session(home, "12345", record["session_id"], nope="x")
    with pytest.raises(ValueError):
        ss.update_session(home, "12345", record["session_id"], status="flying")
    updated = ss.update_session(home, "12345", record["session_id"],
                                status="running", phase="working",
                                progress_message_id="99")
    assert updated["status"] == "running"
    assert updated["phase"] == "working"
    assert updated["progress_message_id"] == "99"


def test_load_refuses_other_owner(tmp_path):
    home = _home(tmp_path)
    record = ss.create_session(home, "12345", kind="delegation", source_id="d1",
                               goal="g", chat_id="42", chat_type="dm")
    # Another owner cannot read the record at all: the per-user directory
    # layout fails closed (missing path) before any record is parsed.
    with pytest.raises((FileNotFoundError, PermissionError)):
        ss.load_session(home, "99999", record["session_id"])


def test_invalid_session_id_rejected(tmp_path):
    home = _home(tmp_path)
    with pytest.raises(ValueError):
        ss.load_session(home, "12345", "not-a-session-id")
    with pytest.raises(ValueError):
        ss.load_session(home, "12345", "../escape")


def test_list_sessions_newest_first_and_limited(tmp_path):
    home = _home(tmp_path)
    ids = []
    for n in range(3):
        record = ss.create_session(home, "12345", kind="delegation", source_id=f"d{n}",
                                   goal=f"goal {n}", chat_id="42", chat_type="dm")
        ids.append(record["session_id"])
        time.sleep(0.01)
        ss.update_session(home, "12345", record["session_id"], phase=f"step {n}")
    rows = ss.list_sessions(home, "12345")
    assert [row["session_id"] for row in rows] == list(reversed(ids))
    assert len(ss.list_sessions(home, "12345", limit=2)) == 2
    assert ss.list_sessions(home, "99999") == []


def test_find_session_for_topic(tmp_path):
    home = _home(tmp_path)
    record = ss.create_session(home, "12345", kind="kanban", source_id="t_root",
                               goal="g", chat_id="42", chat_type="dm", thread_id="777")
    found = ss.find_session_for_topic(home, "42", "777")
    assert found is not None and found["session_id"] == record["session_id"]
    assert ss.find_session_for_topic(home, "42", "778") is None
    assert ss.find_session_for_topic(home, "42", None) is None


@pytest.fixture(autouse=True)
def _clean_delegations():
    from tools import async_delegation as ad
    from tools.process_registry import process_registry
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def test_collect_live_entries_lists_running_delegation(tmp_path):
    from tools import async_delegation as ad
    home = _home(tmp_path)
    gate = threading.Event()

    def blocked_runner():
        gate.wait()
        return {}

    res = ad.dispatch_async_delegation(
        goal="live child work", context=None, toolsets=None, role="leaf",
        model="m", session_key="agent:main:test:dm:1", max_async_children=1,
        runner=blocked_runner, interrupt_fn=gate.set,
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"
    try:
        entries = ss.collect_swarm_entries(home, "12345", "42")
        match = [e for e in entries if e["source_id"] == res["delegation_id"]]
        assert len(match) == 1
        assert match[0]["kind"] == "delegation"
        assert match[0]["status"] in {"running", "stalling"}
        assert "live child work" in match[0]["goal"]
    finally:
        gate.set()


def test_collect_live_entries_merges_durable_record_with_live_delegation(tmp_path):
    from tools import async_delegation as ad
    home = _home(tmp_path)
    gate = threading.Event()

    def blocked_runner():
        gate.wait()
        return {}

    res = ad.dispatch_async_delegation(
        goal="live child work", context=None, toolsets=None, role="leaf",
        model="m", session_key="agent:main:test:dm:1", max_async_children=1,
        runner=blocked_runner, interrupt_fn=gate.set,
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"
    ss.create_session(home, "12345", kind="delegation", source_id=res["delegation_id"],
                      goal="live child work", chat_id="42", chat_type="dm")
    try:
        entries = ss.collect_swarm_entries(home, "12345", "42")
        match = [e for e in entries if e["source_id"] == res["delegation_id"]]
        assert len(match) == 1
        assert match[0]["durable_session_id"] is not None
        assert match[0]["status"] in {"running", "stalling"}
    finally:
        gate.set()


def test_collect_live_entries_lists_durable_record_without_live_process(tmp_path):
    home = _home(tmp_path)
    record = ss.create_session(home, "12345", kind="kanban", source_id="t_root9",
                               goal="durable swarm", chat_id="42", chat_type="dm")
    entries = ss.collect_swarm_entries(home, "12345", "42")
    match = [e for e in entries if e["source_id"] == "t_root9"]
    assert len(match) == 1
    assert match[0]["kind"] == "kanban"
    assert match[0]["durable_session_id"] == record["session_id"]
    assert match[0]["goal"] == "durable swarm"


def test_collect_live_entries_scoped_to_chat(tmp_path):
    home = _home(tmp_path)
    ss.create_session(home, "12345", kind="kanban", source_id="t_other",
                      goal="other chat", chat_id="99", chat_type="group")
    entries = ss.collect_swarm_entries(home, "12345", "42")
    assert all(e["source_id"] != "t_other" for e in entries)
