import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.deep_research_sessions import (
    create_session,
    claim_session_for_resume,
    load_checkpoint,
    list_sessions,
    load_session,
    resolve_session_dir,
    save_checkpoint,
    update_checkpoint_metadata,
    update_session,
)


def test_session_storage_is_private_owner_scoped_and_lifecycle_updates_are_limited(tmp_path):
    home = tmp_path / "profile"
    record = create_session(
        home, "12345", "Research the Attock Fort",
        chat_id="-100123", chat_type="dm", thread_id="77",
    )
    directory = resolve_session_dir(home, "12345", record["session_id"])
    info_path = directory / "session.json"

    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(info_path.stat().st_mode) == 0o600
    assert record["max_turns"] == 150
    assert record["status"] == "starting"
    assert list_sessions(home, "12345")[0]["session_id"] == record["session_id"]
    assert list_sessions(home, "67890") == []

    updated = update_session(
        home, "12345", record["session_id"], status="running", progress_message_id="99",
    )
    assert updated["status"] == "running"
    assert updated["progress_message_id"] == "99"
    with pytest.raises(ValueError, match="Unsupported"):
        update_session(home, "12345", record["session_id"], query="replace the owner query")
    with pytest.raises(ValueError, match="Invalid"):
        update_session(home, "12345", record["session_id"], status="unknown")

    data = json.loads(info_path.read_text(encoding="utf-8"))
    data["user_id"] = "67890"
    info_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PermissionError, match="owner mismatch"):
        load_session(home, "12345", record["session_id"])


def test_session_path_cannot_follow_a_symlink_outside_the_owner_folder(tmp_path):
    home = tmp_path / "profile"
    owner_dir = home / "deep_research_sessions" / "12345"
    owner_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    session_id = "a" * 12
    (owner_dir / session_id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escaped"):
        resolve_session_dir(home, "12345", session_id)


def test_checkpoint_persists_session_folder_topic_trigger_latest_reply_and_status(tmp_path):
    home = tmp_path / "profile"
    record = create_session(
        home, "12345", "Research the Attock Fort", chat_id="-100123",
        chat_type="group", trigger_message_id="400",
    )
    update_session(home, "12345", record["session_id"], status="running", thread_id="77",
                   latest_reply_message_id="401", progress_message_id="402")
    update_checkpoint_metadata(home, "12345", record["session_id"],
                               latest_reply_message_id="401", progress_message_id="402")
    save_checkpoint(home, "12345", record["session_id"], todos=[{"id": "sources", "status": "in_progress"}],
                    phase="sources", status="running")

    checkpoint = load_checkpoint(home, "12345", record["session_id"])
    assert checkpoint["todos"] == [{"id": "sources", "status": "in_progress"}]
    assert checkpoint["phase"] == "sources"
    assert checkpoint["session_dir"] == str(resolve_session_dir(home, "12345", record["session_id"]))
    assert checkpoint["chat_id"] == "-100123"
    assert checkpoint["thread_id"] == "77"
    assert checkpoint["trigger_message_id"] == "400"
    assert checkpoint["latest_reply_message_id"] == "401"
    assert checkpoint["progress_message_id"] == "402"
    assert checkpoint["status"] == "running"


def test_resume_claim_is_atomic_across_concurrent_callers(tmp_path):
    home = tmp_path / "profile"
    record = create_session(
        home, "12345", "Research topic", chat_id="-100123", chat_type="group", thread_id="77",
    )
    update_session(home, "12345", record["session_id"], status="paused")
    save_checkpoint(home, "12345", record["session_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda _: claim_session_for_resume(home, "12345", record["session_id"]),
            range(2),
        ))

    assert sum(claim is not None for claim in claims) == 1
    assert load_session(home, "12345", record["session_id"])["status"] == "starting"
    assert load_checkpoint(home, "12345", record["session_id"])["status"] == "starting"


def test_session_config_fields_are_stored_validated_and_updatable(tmp_path):
    from gateway.deep_research_sessions import (
        DEFAULT_MAX_TURNS,
        DEFAULT_REPORT_FORMAT,
        DEFAULT_RESEARCH_DEPTH,
        MAX_TURNS_CAP,
        default_research_plan,
    )

    home = tmp_path / "profile"
    record = create_session(
        home, "12345", "Configured research", chat_id="-100123", chat_type="dm",
        max_turns=300, report_format="pdf", depth="exhaustive",
        custom_instructions="Focus on primary sources.",
    )
    assert record["max_turns"] == 300
    assert record["report_format"] == "pdf"
    assert record["depth"] == "exhaustive"
    assert record["custom_instructions"] == "Focus on primary sources."

    defaults = create_session(home, "12345", "Default research", chat_id="-100123", chat_type="dm")
    assert defaults["max_turns"] == DEFAULT_MAX_TURNS == 150
    assert defaults["report_format"] == DEFAULT_REPORT_FORMAT == "md"
    assert defaults["depth"] == DEFAULT_RESEARCH_DEPTH == "deep"

    updated = update_session(
        home, "12345", record["session_id"],
        max_turns=50, report_format="docx", depth="standard", custom_instructions="",
    )
    assert (updated["max_turns"], updated["report_format"], updated["depth"]) == (50, "docx", "standard")
    assert updated["custom_instructions"] == ""

    for bad_kwargs in (
        {"max_turns": 0}, {"max_turns": MAX_TURNS_CAP + 1}, {"max_turns": "many"},
        {"report_format": "txt"}, {"depth": "shallow"},
    ):
        with pytest.raises(ValueError):
            create_session(home, "12345", "Bad config", chat_id="-100123", chat_type="dm", **bad_kwargs)
        with pytest.raises(ValueError):
            update_session(home, "12345", record["session_id"], **bad_kwargs)

    plan = default_research_plan()
    assert [task["id"] for task in plan] == ["dr-scope", "dr-search", "dr-read-verify", "dr-report", "dr-verify"]
    assert all(task["status"] == "pending" for task in plan)


def test_resume_accept_current_waives_only_artifact_drift(tmp_path):
    from gateway.deep_research_sessions import validate_resume_state

    home = tmp_path / "profile"
    record = create_session(home, "12345", "Drifted research", chat_id="-100123", chat_type="dm", thread_id="77")
    session_id = record["session_id"]
    directory = resolve_session_dir(home, "12345", session_id)
    save_checkpoint(home, "12345", session_id, status="paused")
    # Files written after the checkpoint are newer than their snapshots.
    (directory / "research_notes.md").write_text("# Notes\nNewer content.\n", encoding="utf-8")
    (directory / "report.md").write_text("# Report\nNewer content.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="differs from its checkpoint snapshot"):
        validate_resume_state(home, "12345", session_id)
    accepted = validate_resume_state(home, "12345", session_id, accept_artifact_drift=True)
    assert accepted["session_id"] == session_id

    # Identity drift is never waived by the owner-reconcile flag. Edit the
    # session record directly so the checkpoint keeps the original identity
    # (update_session would sync both files and hide the discrepancy).
    session_path = directory / "session.json"
    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    session_data["chat_id"] = "-100999"
    session_path.write_text(json.dumps(session_data), encoding="utf-8")
    with pytest.raises(ValueError, match="differs between session.json and checkpoint.json"):
        validate_resume_state(home, "12345", session_id, accept_artifact_drift=True)
