import json

import pytest

from gateway.deep_research_sessions import (
    create_session, load_checkpoint, load_session, resolve_session_dir,
    save_checkpoint, update_session, validate_resume_state,
)


def saved(tmp_path):
    record = create_session(tmp_path, "12345", "Original question", chat_id="42", chat_type="dm", thread_id="777")
    sid = record["session_id"]
    update_session(tmp_path, "12345", sid, status="paused", latest_reply_message_id="900")
    directory = resolve_session_dir(tmp_path, "12345", sid)
    (directory / "research_notes.md").write_text("Original source ledger")
    (directory / "report.md").write_text("Original draft")
    save_checkpoint(tmp_path, "12345", sid,
                    todos=[{"id": "a", "content": "Remaining task", "status": "in_progress", "elapsed_seconds": 13}],
                    research_evidence={"searches": 21, "result_urls": ["https://example.com/a"], "read_urls": []},
                    tool_history=[{"name": "web_search", "success": True, "output": "source result"}],
                    last_error="Prior failure")
    return sid, directory


@pytest.mark.parametrize("case", ["missing", "corrupt", "topic", "status", "delivery", "draft", "ledger", "tasks", "evidence", "source_type", "source_unread", "query", "identity"])
def test_resume_discrepancies_block_without_changing_saved_files(tmp_path, case):
    sid, directory = saved(tmp_path)
    path = directory / "checkpoint.json"
    checkpoint = load_checkpoint(tmp_path, "12345", sid)
    if case == "missing":
        path.unlink()
    elif case == "corrupt":
        path.write_text("{broken")
    elif case in {"draft", "ledger"}:
        (directory / ("report.md" if case == "draft" else "research_notes.md")).write_text("Changed outside checkpoint")
    elif case in {"source_type", "source_unread"}:
        checkpoint["research_evidence"]["read_sources"] = (None if case == "source_type" else [
            {"url": "https://unread.example/", "host": "unread.example", "title": "Not read"}])
        path.write_text(json.dumps(checkpoint))
    else:
        key, value = {
            "topic": ("thread_id", "888"), "status": ("status", "completed"),
            "delivery": ("final_response_status", "delivered"), "tasks": ("todos", "invalid"),
            "evidence": ("research_evidence", {"searches": -1}), "query": ("query", "Different question"),
            "identity": ("session_id", "b" * 12),
        }[case]
        checkpoint[key] = value
        path.write_text(json.dumps(checkpoint))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ValueError):
        validate_resume_state(tmp_path, "12345", sid)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert load_session(tmp_path, "12345", sid)["status"] == "paused"


def test_valid_resume_preserves_complete_durable_context(tmp_path):
    sid, _ = saved(tmp_path)
    checkpoint = validate_resume_state(tmp_path, "12345", sid)
    assert checkpoint["research_evidence"]["searches"] == 21
    assert checkpoint["todos"][0]["elapsed_seconds"] == 13
    assert checkpoint["source_ledger"]["content"] == "Original source ledger"
    assert checkpoint["partial_report"]["content"] == "Original draft"
    assert checkpoint["last_error"] == "Prior failure"
    assert checkpoint["tool_history"][0]["success"] is True


def test_direct_resume_claim_also_refuses_corrupt_state(tmp_path):
    from gateway.deep_research_sessions import claim_session_for_resume
    sid, directory = saved(tmp_path)
    (directory / "report.md").write_text("Unreconciled report")
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(ValueError, match="report.md differs"):
        claim_session_for_resume(tmp_path, "12345", sid)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_metadata_refresh_never_replaces_wrong_checkpoint(tmp_path):
    from gateway.deep_research_sessions import update_checkpoint_metadata
    sid, directory = saved(tmp_path)
    path = directory / "checkpoint.json"
    path.write_text(json.dumps({"session_id": "b" * 12, "todos": ["valuable work"]}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="refusing to replace"):
        update_checkpoint_metadata(tmp_path, "12345", sid)
    assert path.read_bytes() == before


def test_legacy_source_counts_without_tool_history_require_owner_review(tmp_path):
    sid, directory = saved(tmp_path)
    path = directory / "checkpoint.json"
    data = json.loads(path.read_text())
    data.pop("tool_history")
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="prior tool-outcome history is missing"):
        validate_resume_state(tmp_path, "12345", sid)
    assert path.read_bytes() == before


def test_topic_lookup_is_exact_and_cross_owner_without_mutation(tmp_path):
    from gateway.deep_research_sessions import find_session_for_topic
    sid, directory = saved(tmp_path)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    found = find_session_for_topic(tmp_path, "42", "777")
    assert found["session_id"] == sid
    assert found["user_id"] == "12345"
    assert find_session_for_topic(tmp_path, "42", None) is None
    assert find_session_for_topic(tmp_path, "42", "888") is None
    assert find_session_for_topic(tmp_path, "99", "777") is None
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    create_session(tmp_path, "99999", "Other owner's session", chat_id="42", chat_type="group", thread_id="777")
    with pytest.raises(ValueError, match="Multiple research sessions"):
        find_session_for_topic(tmp_path, "42", "777")


def test_saving_never_replaces_invalid_checkpoint(tmp_path):
    sid, directory = saved(tmp_path)
    path = directory / "checkpoint.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="refusing to replace"):
        save_checkpoint(tmp_path, "12345", sid, todos=[])
    assert path.read_text() == "[]"
