from types import SimpleNamespace

import pytest

from gateway.deep_research_sessions import load_checkpoint, load_session, resolve_session_dir
from gateway.run_turn_runner import TurnRunner
from tests.gateway.test_deep_research_progress import _runner
from tools.todo_tool import TodoStore, TodoLifecycleError


def prepare(home, *, status="completed", artifacts=True):
    runner = _runner(home)
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()
    async def no_card(**kwargs):
        pass
    runner._request_search_progress = no_card
    runner._search_progress_todos = ([] if status is None else [
        {"id": "verify", "content": "Verify report", "status": status, "elapsed_seconds": 12.5}])
    runner._deep_research_tool_history = [
        {"name": "terminal", "success": False, "output_excerpt": "Skipped: TTL expired", "truncated": False}]
    sid = runner._ctx.deep_research["session_id"]
    if artifacts:
        urls = ["https://agency.example/one", "https://university.example/two"]
        runner._search_metrics["searches"] = 21
        runner._search_metrics["result_urls"].update(urls)
        runner._search_metrics["pages_read"].update(urls)
        directory = resolve_session_dir(home, "12345", sid)
        (directory / "report.md").write_text("Findings [1] and [2].\n\n## References\n[1] " + urls[0] + "\n[2] " + urls[1])
        (directory / "research_notes.md").write_text("Sources actually read\n" + "\n".join(urls))
    return runner, sid


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "in_progress", None])
async def test_model_success_claim_replaced_before_delivery_when_tasks_unfinished(tmp_path, status):
    runner, sid = prepare(tmp_path, status=status)
    model_claim = "Research is complete and the report was delivered."
    outgoing = {"final_response": model_claim, "completed": True}
    runner._ctx.result_holder[0] = {"final_response": model_claim, "completed": True}
    await runner.finish_search_progress(success=True, result=outgoing)
    session = load_session(tmp_path, "12345", sid)
    assert session["status"] == "paused"
    assert session["artifact_status"] == "ready"
    assert session["final_response_status"] == "failed"
    assert outgoing["research_completion_blocked"] is True
    assert outgoing["completed"] is False
    assert model_claim not in outgoing["final_response"]
    assert "not confirmed complete or delivered" in outgoing["final_response"]
    assert runner._ctx.result_holder[0]["final_response"] == outgoing["final_response"]
    checkpoint = load_checkpoint(tmp_path, "12345", sid)
    assert checkpoint["research_evidence"]["searches"] == 21
    assert checkpoint["tool_history"][0]["success"] is False


@pytest.mark.asyncio
async def test_invalid_artifacts_cannot_publish_model_completion_claim(tmp_path):
    runner, sid = prepare(tmp_path, artifacts=False)
    result = {"final_response": "Research complete. All evidence verified.", "completed": True}
    await runner.finish_search_progress(success=True, result=result)
    assert load_session(tmp_path, "12345", sid)["status"] == "paused"
    assert "source-evidence checks" in result["final_response"]
    assert result["completed"] is False


@pytest.mark.asyncio
async def test_valid_report_and_finished_tasks_are_ready_not_delivered(tmp_path):
    runner, sid = prepare(tmp_path)
    result = {"final_response": "Research complete and report delivered.", "completed": True}
    await runner.finish_search_progress(success=True, result=result)
    session = load_session(tmp_path, "12345", sid)
    assert session["status"] == "running"
    assert session["artifact_status"] == "ready"
    assert session["final_response_status"] == "pending"
    assert "delivery in this topic is still pending" in result["final_response"]
    assert result["research_completion_blocked"] is False


@pytest.mark.asyncio
async def test_persistence_failure_suppresses_success_claim_and_pauses(tmp_path, monkeypatch):
    import gateway.deep_research_sessions as sessions
    runner, sid = prepare(tmp_path)
    def fail_checkpoint(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(sessions, "save_checkpoint", fail_checkpoint)
    result = {"final_response": "Research complete and delivered.", "completed": True}
    await runner.finish_search_progress(success=True, result=result)
    assert "checks could not be verified" in result["final_response"]
    assert result["completed"] is False
    assert load_session(tmp_path, "12345", sid)["status"] == "paused"


def test_deepresearch_model_narration_cannot_stream_ahead_of_validation(tmp_path):
    runner, _ = prepare(tmp_path)
    assert runner._setup_stream_consumer("telegram") == (None, None, None, False, None)


def test_failed_readback_revokes_prior_successful_write_credit():
    store = TodoStore(strict_lifecycle=True)
    task = {"id": "verify", "content": "Read back the report", "status": "in_progress"}
    store.write([task])
    store.note_work_result(success=True)
    store.note_work_result(success=False)
    with pytest.raises(TodoLifecycleError, match="no successful tool work"):
        store.write([{**task, "status": "completed"}])
    store.note_work_result(success=True)
    assert store.write([{**task, "status": "completed"}])[0]["status"] == "completed"


def test_one_tool_call_cannot_verify_multiple_unrelated_running_tasks():
    store = TodoStore(strict_lifecycle=True)
    tasks = [{"id": "a", "content": "Read source A", "status": "in_progress"},
             {"id": "b", "content": "Verify report B", "status": "in_progress"}]
    store.write(tasks)
    store.note_work_result(success=True)
    with pytest.raises(TodoLifecycleError, match="no successful tool work"):
        store.write([{**tasks[0], "status": "completed"}, tasks[1]])


def test_resumed_ledger_mismatch_requires_review_without_restoring_or_running(tmp_path):
    runner, _ = prepare(tmp_path)
    durable = [{"id": "a", "content": "Remaining research", "status": "in_progress", "elapsed_seconds": 22}]
    runner._ctx.deep_research.update(resumed=True, checkpoint={"todos": durable})
    store = TodoStore(strict_lifecycle=True)
    store.restore(durable)
    agent = SimpleNamespace(_todo_store=store, _latest_todo_response=lambda _history: (
        [{"id": "a", "content": "Remaining research", "status": "completed"}], 5))
    before = store.snapshot()
    issue = runner._prepare_research_todo_resume(agent, [])
    assert "differs" in issue
    assert "tell me which durable state" in issue
    assert store.snapshot()["todos"][0]["status"] == before["todos"][0]["status"]
    assert store.snapshot()["revision"] == before["revision"]


def test_matching_history_cannot_override_saved_task_timing_on_resume(tmp_path):
    runner, _ = prepare(tmp_path)
    durable = [{"id": "a", "content": "Remaining research", "status": "in_progress", "elapsed_seconds": 22}]
    runner._ctx.deep_research.update(resumed=True, checkpoint={"todos": durable})
    store = TodoStore(strict_lifecycle=True)
    agent = SimpleNamespace(_todo_store=store, _latest_todo_response=lambda _history: (
        [{"id": "a", "content": "Remaining research", "status": "in_progress"}], 19))
    assert runner._prepare_research_todo_resume(agent, []) is None
    assert store.snapshot()["revision"] == 19
    assert store.snapshot()["todos"][0]["elapsed_seconds"] >= 22
    with pytest.raises(TodoLifecycleError, match="no successful tool work"):
        store.write([{"id": "a", "content": "Remaining research", "status": "completed"}])
