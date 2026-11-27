"""Checkpoint-gate behaviour contracts (audit 5.35/6.25): gates pause
durably, decisions resume/revise/reject exactly as recorded, unknown
gates/decisions fail, and a 30-minute timeout is a durable deny — never
an auto-approve, never a re-ask.
"""
from datetime import UTC, datetime, timedelta

import pytest

from gateway.web_designer import checkpoints, machine, store


def _running_record(tmp_path):
    record = store.create_run(home=str(tmp_path / "home"), thread_id="t1",
                              user_id="u1", chat_id="c1")
    store.start_run(record)
    store.complete_stage(record, "load_gate", data={"decision": "load"})
    record.stage = "intake"
    store.save_run(record)
    return record


def _advance(record, stage, data):
    machine.advance(record, stage, data=data)


def test_a_gate_pauses_durably_and_approve_resumes(tmp_path):
    record = _running_record(tmp_path)
    _advance(record, "intake",
             {"purpose": "p", "audience": "a", "primary_action": "go"})
    checkpoints.request_checkpoint(record, "brief", payload={"brief": "v1"})
    assert record.status == "awaiting_checkpoint"
    assert record.stage_data["pending_checkpoint"]["gate"] == "brief"
    reloaded = store.load_run(str(tmp_path / "home"), record.run_id)
    assert reloaded.status == "awaiting_checkpoint"
    checkpoints.record_decision(record, "brief", "approve")
    assert record.status == "running"
    assert record.checkpoint_decisions[0]["decision"] == "approve"


def test_unknown_gates_and_decisions_fail(tmp_path):
    record = _running_record(tmp_path)
    with pytest.raises(ValueError):
        checkpoints.request_checkpoint(record, "delivery")
    with pytest.raises(ValueError):
        checkpoints.record_decision(record, "brief", "approve")
    checkpoints.request_checkpoint(record, "brief")
    with pytest.raises(ValueError):
        checkpoints.record_decision(record, "brief", "maybe")


def test_approve_with_changes_needs_changes_and_reopens_the_stage(tmp_path):
    record = _running_record(tmp_path)
    _advance(record, "intake",
             {"purpose": "p", "audience": "a", "primary_action": "go"})
    _advance(record, "brief",
             {"problem": "p", "audience": "a", "primary_action": "go",
              "success_measure": "m", "tone": "calm", "non_goals": [],
              "intent": "i"})
    _advance(record, "scope_freeze",
             {"pages": ["home"], "features": [], "out_of_scope": [],
              "delivery_form": "static site"})
    _advance(record, "research",
             {"note": "n", "sources": ["https://example.com"]})
    _advance(record, "ia",
             {"sitemap": ["home"], "navigation": ["home"], "flows": ["f"]})
    checkpoints.request_checkpoint(record, "wireframe")
    with pytest.raises(ValueError):
        checkpoints.record_decision(record, "wireframe",
                                    "approve_with_changes")
    checkpoints.record_decision(
        record, "wireframe", "approve_with_changes",
        changes=["add a pricing section"], return_stage="wireframe")
    assert record.status == "running"
    assert record.stage == "wireframe"
    assert "wireframe" not in record.completed_stages
    machine.advance(record, "wireframe",
                    data={"pages": [{"name": "home", "sections": ["hero"]}]})
    assert record.stage == "copy"


def test_reject_returns_to_the_gate_stage_with_the_reason(tmp_path):
    record = _running_record(tmp_path)
    _advance(record, "intake",
             {"purpose": "p", "audience": "a", "primary_action": "go"})
    checkpoints.request_checkpoint(record, "brief")
    checkpoints.record_decision(record, "brief", "reject",
                                changes="wrong audience", return_stage="brief")
    assert record.status == "running"
    assert record.stage == "brief"
    decision = record.checkpoint_decisions[0]
    assert decision["decision"] == "reject"
    assert decision["reason"] == "wrong audience"


def test_a_timeout_is_a_durable_deny_not_an_approval(tmp_path):
    record = _running_record(tmp_path)
    checkpoints.request_checkpoint(record, "brief")
    assert checkpoints.check_timeout(record, "brief") is False
    past = datetime.now(UTC) - timedelta(minutes=31)
    record.stage_data["pending_checkpoint"]["requested_at"] = (
        past.isoformat(timespec="seconds"))
    store.save_run(record)
    assert checkpoints.check_timeout(record, "brief") is True
    assert record.status == "paused"
    assert record.checkpoint_decisions[0]["decision"] == "timeout_deny"
    # Never re-asked: nothing pending any more.
    assert checkpoints.check_timeout(record, "brief") is False
    reloaded = store.load_run(str(tmp_path / "home"), record.run_id)
    assert reloaded.status == "paused"
