"""Tests for the todo tool module."""

import json
import time

from tools.todo_tool import TodoStore, todo_tool

class TestWriteAndRead:
    def test_write_replaces_list(self):
        store = TodoStore()
        items = [
            {"id": "1", "content": "First task", "status": "pending"},
            {"id": "2", "content": "Second task", "status": "in_progress"},
        ]
        result = store.write(items)
        assert len(result) == 2
        assert result[0]["id"] == "2"
        assert result[0]["status"] == "in_progress"
        assert result[1]["id"] == "1"

    def test_write_deduplicates_duplicate_ids(self):
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "First version", "status": "pending"},
            {"id": "2", "content": "Other task", "status": "pending"},
            {"id": "1", "content": "Latest version", "status": "in_progress"},
        ])
        assert [{k: item[k] for k in ("id", "content", "status")} for item in result] == [
            {"id": "1", "content": "Latest version", "status": "in_progress"},
            {"id": "2", "content": "Other task", "status": "pending"},
        ]

    def test_write_moves_active_item_before_earlier_pending_step(self):
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "Already done", "status": "completed"},
            {"id": "2", "content": "Verify freed space", "status": "pending"},
            {"id": "3", "content": "Move archives to Trash", "status": "in_progress"},
        ])
        assert [{k: item[k] for k in ("id", "content", "status")} for item in result] == [
            {"id": "1", "content": "Already done", "status": "completed"},
            {"id": "3", "content": "Move archives to Trash", "status": "in_progress"},
            {"id": "2", "content": "Verify freed space", "status": "pending"},
        ]

class TestHasItems:
    def test_empty_store(self):
        store = TodoStore()
        assert store.has_items() is False

    def test_non_empty_store(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "x", "status": "pending"}])
        assert store.has_items() is True

class TestFormatForInjection:
    def test_empty_returns_none(self):
        store = TodoStore()
        assert store.format_for_injection() is None

    def test_non_empty_has_markers(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Do thing", "status": "completed"},
            {"id": "2", "content": "Next", "status": "pending"},
            {"id": "3", "content": "Working", "status": "in_progress"},
        ])
        text = store.format_for_injection()
        # Completed items are filtered out of injection
        assert "[x]" not in text
        assert "Do thing" not in text
        # Active items are included
        assert "[ ]" in text
        assert "[>]" in text
        assert "Next" in text
        assert "Working" in text

class TestMergeMode:
    def test_update_existing_by_id(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Original", "status": "pending"},
        ])
        store.write(
            [{"id": "1", "status": "completed"}],
            merge=True,
        )
        items = store.read()
        assert len(items) == 1
        assert items[0]["status"] == "completed"
        assert items[0]["content"] == "Original"

    def test_merge_appends_new(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "First", "status": "pending"}])
        store.write(
            [{"id": "2", "content": "Second", "status": "pending"}],
            merge=True,
        )
        items = store.read()
        assert len(items) == 2

    def test_merge_reorders_active_item_ahead_of_earlier_pending_step(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Completed", "status": "completed"},
            {"id": "2", "content": "Verify freed space", "status": "pending"},
            {"id": "3", "content": "Move archives to Trash", "status": "pending"},
        ])
        result = store.write(
            [{"id": "3", "status": "in_progress"}],
            merge=True,
        )
        assert [{k: item[k] for k in ("id", "content", "status")} for item in result] == [
            {"id": "1", "content": "Completed", "status": "completed"},
            {"id": "3", "content": "Move archives to Trash", "status": "in_progress"},
            {"id": "2", "content": "Verify freed space", "status": "pending"},
        ]

class TestTodoToolFunction:
    def test_read_mode(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "Task", "status": "pending"}])
        result = json.loads(todo_tool(store=store))
        assert result["summary"]["total"] == 1
        assert result["summary"]["pending"] == 1
        assert result["revision"] == 1

    def test_no_store_returns_error(self):
        result = json.loads(todo_tool())
        assert "error" in result

class TestTodoStoreSnapshots:
    def test_revision_only_advances_when_state_changes(self):
        store = TodoStore()
        items = [{"id": "1", "content": "Task", "status": "pending"}]

        store.write(items)
        first = store.snapshot()
        store.write(items)

        assert first["revision"] == 1
        assert store.snapshot() == first

    def test_restore_adopts_a_trusted_revision(self):
        store = TodoStore()
        store.restore(
            [{"id": "1", "content": "Task", "status": "pending"}], revision=7
        )

        assert store.snapshot()["revision"] == 7

        store.write([{"id": "1", "content": "Task", "status": "completed"}])
        assert store.snapshot()["revision"] == 8


class TestTodoRuntime:
    def test_server_tracks_elapsed_and_ignores_model_supplied_runtime(self):
        store = TodoStore()
        running = store.write([{
            "id": "t", "content": "Do work", "status": "in_progress",
            "started_at": "1900-01-01T00:00:00+00:00", "elapsed_seconds": 999999,
            "completed_at": "1900-01-02T00:00:00+00:00",
        }])[0]
        assert running["started_at"] != "1900-01-01T00:00:00+00:00"
        assert running["elapsed_seconds"] < 10
        time.sleep(0.01)
        completed = store.write([{"id": "t", "status": "completed",
                                  "elapsed_seconds": 0, "completed_at": "forged"}])[0]
        assert completed["started_at"] == running["started_at"]
        assert completed["elapsed_seconds"] >= 0.01
        assert completed["completed_at"] != "forged"
        assert "in_progress" not in completed["status"]

    def test_replace_preserves_runtime_and_restore_excludes_downtime(self):
        store = TodoStore()
        first = store.write([{"id": "t", "content": "Do work", "status": "in_progress"}])[0]
        time.sleep(0.02)
        snapshot = store.snapshot()
        saved_elapsed = snapshot["todos"][0]["elapsed_seconds"]
        time.sleep(0.06)  # simulated process downtime between persistence and restore
        restored = TodoStore()
        restored.restore(snapshot["todos"], revision=snapshot["revision"])
        time.sleep(0.015)
        resumed = restored.write([{"id": "t", "content": "Updated", "status": "in_progress"}])[0]
        assert resumed["started_at"] == first["started_at"]
        assert saved_elapsed <= resumed["elapsed_seconds"] < saved_elapsed + 0.05

    def test_merge_preserves_runtime_fields(self):
        store = TodoStore()
        running = store.write([{"id": "t", "content": "Original", "status": "in_progress"}])[0]
        merged = store.write([{"id": "t", "content": "Reworded", "status": "in_progress"}], merge=True)[0]
        assert merged["started_at"] == running["started_at"]
        assert merged["elapsed_seconds"] >= running["elapsed_seconds"]


class TestStrictTodoLifecycle:
    def test_rejects_new_or_pending_completion_atomically_with_snapshot(self):
        store = TodoStore(strict_lifecycle=True)
        store.write([{"id": "old", "content": "Keep", "status": "pending"}])
        before = store.snapshot()
        out = json.loads(todo_tool(todos=[
            {"id": "old", "content": "Keep", "status": "completed"},
            {"id": "new", "content": "Catch-up", "status": "completed"},
        ], store=store))
        assert "error" in out
        assert "snapshot" in out and out["snapshot"] == before
        assert store.snapshot() == before

    def test_requires_successful_tool_work_then_allows_one_completion(self):
        store = TodoStore(strict_lifecycle=True)
        store.write([{"id": "a", "content": "Work", "status": "in_progress"}])
        blocked = json.loads(todo_tool(todos=[{"id": "a", "status": "completed"}], store=store))
        assert "successful tool work" in blocked["error"]
        store.note_work_result(success=True)
        done = json.loads(todo_tool(todos=[{"id": "a", "status": "completed"}], store=store))
        assert done["todos"][0]["status"] == "completed"
        assert done["todos"][0]["elapsed_seconds"] >= 0

    def test_rejects_bulk_catch_up_atomically(self):
        store = TodoStore(strict_lifecycle=True)
        store.write([{"id": key, "content": key, "status": "in_progress"} for key in ("a", "b")])
        store.note_work_result(success=True)
        before = store.snapshot()
        result = json.loads(todo_tool(todos=[
            {"id": "a", "status": "completed"}, {"id": "b", "status": "completed"}
        ], store=store))
        assert "at most one" in result["error"]
        assert store.snapshot() == before

    def test_replay_of_work_evidence_is_ignored_on_write_but_restored_from_snapshot(self):
        store = TodoStore(strict_lifecycle=True)
        store.write([{"id": "a", "content": "Work", "status": "in_progress"}])
        store.note_work_result(success=True)
        state = store.snapshot()
        resumed = TodoStore(strict_lifecycle=True)
        resumed.restore(state["todos"], revision=state["revision"])
        resumed.write([{"id": "a", "status": "in_progress", "work_observed": False,
                        "started_at": "1900-01-01", "elapsed_seconds": 999999}])
        result = json.loads(todo_tool(todos=[{"id": "a", "status": "completed"}], store=resumed))
        assert result["todos"][0]["status"] == "completed"
class TestTodoStoreBounds:
    """Bounds on persisted todo state (GHSA-5g4g-6jrg-mw3g hardening).

    The todo list is re-injected into context after every compression event,
    so an unbounded item — whether authored by the model or replayed from
    caller-supplied history on the API server's _hydrate_todo_store path —
    would defeat the compression it rides through. These pin the caps.
    Not a security boundary (the API surface is authenticated and the caller
    supplies their own history); this is footgun containment / parity.
    """

    def test_oversized_content_is_truncated(self):
        from tools.todo_tool import MAX_TODO_CONTENT_CHARS
        store = TodoStore()
        store.write([{"id": "1", "content": "A" * 50001, "status": "pending"}])
        item = store.read()[0]
        assert len(item["content"]) <= MAX_TODO_CONTENT_CHARS
        assert item["content"].endswith("… [truncated]")

    def test_injection_block_is_bounded(self):
        from tools.todo_tool import MAX_TODO_CONTENT_CHARS
        store = TodoStore()
        store.write([{"id": "1", "content": "A" * 50001, "status": "pending"}])
        inj = store.format_for_injection()
        # Before the fix this was ~50085 chars; now it tracks the cap.
        assert len(inj) < MAX_TODO_CONTENT_CHARS + 200

    def test_item_count_is_bounded(self):
        from tools.todo_tool import MAX_TODO_ITEMS
        store = TodoStore()
        store.write([
            {"id": str(i), "content": f"task {i}", "status": "pending"}
            for i in range(5000)
        ])
        assert len(store.read()) == MAX_TODO_ITEMS

    def test_normal_list_is_unchanged(self):
        """No regression: ordinary plans pass through untouched (no marker,
        same content, same order)."""
        store = TodoStore()
        store.write([
            {"id": "1", "content": "write the report", "status": "in_progress"},
            {"id": "2", "content": "review PR", "status": "pending"},
        ])
        items = store.read()
        assert [i["content"] for i in items] == ["write the report", "review PR"]
        assert "[truncated]" not in items[0]["content"]


def test_legacy_completed_task_does_not_invent_zero_runtime():
    store = TodoStore()
    legacy = [{"id": "legacy", "content": "Old work", "status": "completed"}]
    restored = store.restore(legacy)
    assert "elapsed_seconds" not in restored[0]
    assert "completed_at" not in restored[0]
    direct = store.write([{"id": "new", "content": "Already done", "status": "completed"}])
    assert "elapsed_seconds" not in direct[0]


class TestRejectEmptyContent:
    """Empty or missing content must fail the call instead of substituting
    '(no description)'. See #44496."""

    def test_write_rejects_empty_content(self):
        store = TodoStore()
        try:
            store.write([{"id": "1", "content": "", "status": "pending"}])
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_write_rejects_missing_content_key(self):
        store = TodoStore()
        try:
            store.write([{"id": "2", "status": "pending"}])
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_write_rejects_whitespace_content(self):
        store = TodoStore()
        try:
            store.write([{"id": "3", "content": "   ", "status": "pending"}])
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_partial_write_does_not_mutate_existing_list(self):
        """A failed write with mixed valid/invalid items leaves the prior list
        untouched (acceptance criterion #3 from #44496)."""
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Keep me", "status": "pending"},
            {"id": "2", "content": "Also keep me", "status": "completed"},
        ])
        try:
            store.write([
                {"id": "1", "content": "Updated", "status": "pending"},  # valid
                {"id": "2", "content": "",        "status": "pending"},  # invalid
            ])
        except ValueError:
            pass
        items = store.read()
        assert [i["content"] for i in items] == ["Keep me", "Also keep me"]
        assert [i["status"]  for i in items] == ["pending", "completed"]

    def test_todo_tool_returns_error_on_empty_content(self):
        result = json.loads(todo_tool(
            todos=[{"id": "1", "content": "", "status": "pending"}],
            store=TodoStore(),
        ))
        assert "error" in result
        assert "empty or missing content" in result["error"]

    def test_merge_new_item_rejects_empty_content(self):
        """Merge mode: new items with empty content are rejected."""
        store = TodoStore()
        store.write([{"id": "1", "content": "Existing", "status": "pending"}])
        try:
            store.write([{"id": "2", "content": "", "status": "pending"}], merge=True)
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_merge_new_item_rejects_whitespace_content(self):
        """Merge mode: new items with whitespace-only content are rejected."""
        store = TodoStore()
        store.write([{"id": "1", "content": "Existing", "status": "pending"}])
        try:
            store.write([{"id": "2", "content": "   ", "status": "pending"}], merge=True)
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_merge_existing_item_keeps_content_on_status_only_update(self):
        """Merge mode: updating only status (no content key) preserves existing content."""
        store = TodoStore()
        store.write([{"id": "1", "content": "Keep this", "status": "pending"}])
        store.write([{"id": "1", "status": "completed"}], merge=True)
        items = store.read()
        assert items[0]["content"] == "Keep this"
        assert items[0]["status"] == "completed"

    def test_valid_content_still_works(self):
        """Regression: valid items still pass through normally."""
        store = TodoStore()
        items = store.write([
            {"id": "1", "content": "A real task", "status": "pending"},
        ])
        assert items[0]["content"] == "A real task"
