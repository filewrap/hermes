"""Approving a staged memory write must never be a dead end.

A batch staged against entries that changed before approval used to fail
all-or-nothing ("Entry changed since it was staged") and keep its pending
record forever, while each new background fork re-staged the same removals.
Approve now applies the ops whose pinned entries are still current, reports
the stale ones as skipped, discards the record, and an identical background
proposal is not staged twice.
"""

import json

import pytest

from tools.memory_tool import MemoryStore, memory_tool
from tools.skill_provenance import reset_current_write_origin, set_current_write_origin
from tools import write_approval as wa
from hermes_cli.write_approval_commands import handle_pending_subcommand

ENTRY_A = "BigBasket reverse-engineering target: Next.js monsters-inc build"
ENTRY_B = "Camofox browser server installed at /opt/camofox-browser (Node 22)"
ENTRY_C = "keep this entry around"


@pytest.fixture()
def home(monkeypatch, tmp_path):
    """HERMES_HOME for the pending store + get_memory_dir for the entries."""
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(d))
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path / "memories")
    (tmp_path / "memories").mkdir(exist_ok=True)
    return d


@pytest.fixture()
def store(home):
    s = MemoryStore(memory_char_limit=2000, user_char_limit=1000)
    s.load_from_disk()
    for entry in (ENTRY_A, ENTRY_B, ENTRY_C):
        assert s.add("memory", entry)["success"]
    return s


def _stage_background_batch(store, operations):
    """Stage a batch exactly as an unattended background-review fork does."""
    token = set_current_write_origin("background_review")
    try:
        result = json.loads(memory_tool(operations=operations, store=store))
    finally:
        reset_current_write_origin(token)
    assert result.get("staged") is True, result
    return result["pending_id"]


def _ops(*old_texts):
    return [{"action": "remove", "old_text": text} for text in old_texts]


class TestApproveStaleBatch:
    def test_applies_current_ops_and_skips_stale(self, store):
        pid = _stage_background_batch(store, _ops("BigBasket reverse", "Camofox browser"))
        assert store.patch("memory", "Camofox browser server", "Camofox browser server (new port)")["success"]

        out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=store)

        assert "Approved 1 memory write(s)." in out
        assert "Skipped" in out and "Camofox browser server installed at" in out
        assert ENTRY_A not in store.entries("memory")  # current op applied
        assert any("Camofox browser server (new port)" in e for e in store.entries("memory"))
        assert wa.get_pending(wa.MEMORY, pid) is None  # record cleared, cannot fail forever

    def test_single_stale_remove_clears_record(self, store):
        token = set_current_write_origin("background_review")
        try:
            result = json.loads(memory_tool(action="remove", old_text="BigBasket reverse", store=store))
        finally:
            reset_current_write_origin(token)
        pid = result["pending_id"]
        assert store.remove("memory", "BigBasket reverse")["success"]  # entry gone before approval

        out = handle_pending_subcommand(wa.MEMORY, ["approve", pid], memory_store=store)

        assert "Approved 1 memory write(s)." in out
        assert "Skipped" in out
        assert wa.get_pending(wa.MEMORY, pid) is None

    def test_approve_all_clears_stale_record(self, store):
        _stage_background_batch(store, _ops("BigBasket reverse", "Camofox browser"))
        assert store.patch("memory", "Camofox browser server", "Camofox browser server (new port)")["success"]

        out = handle_pending_subcommand(wa.MEMORY, ["approve", "all"], memory_store=store)

        assert "Approved 1 memory write(s)." in out
        assert "Failed:" not in out
        assert wa.list_pending(wa.MEMORY) == []


class TestBackgroundRestageDedup:
    def test_identical_batch_not_staged_twice(self, store):
        ops = _ops("BigBasket reverse", "Camofox browser")
        first = _stage_background_batch(store, ops)
        second = _stage_background_batch(store, ops)

        assert second == first
        assert len(wa.list_pending(wa.MEMORY)) == 1

    def test_different_batch_still_stages(self, store):
        first = _stage_background_batch(store, _ops("BigBasket reverse"))
        second = _stage_background_batch(store, _ops("Camofox browser"))

        assert second != first
        assert len(wa.list_pending(wa.MEMORY)) == 2


class TestPartialApplyScope:
    def test_non_stale_failure_still_aborts_whole_batch(self, store):
        """The tolerance is stale pins only: an unmatched op still fails everything."""
        result = store.apply_batch_partial("memory", [
            {"action": "remove", "old_text": "BigBasket reverse"},
            {"action": "remove", "old_text": "entry that never existed"},
        ])
        assert result["success"] is False
        assert ENTRY_A in store.entries("memory")  # nothing applied

    def test_direct_batch_stays_all_or_nothing(self, store):
        """The live tool path keeps its atomic contract; only approve replays partial."""
        pid = _stage_background_batch(store, _ops("BigBasket reverse", "Camofox browser"))
        assert store.patch("memory", "Camofox browser server", "Camofox browser server (new port)")["success"]
        payload = wa.get_pending(wa.MEMORY, pid)["payload"]
        result = store.apply_batch("memory", payload["operations"])
        assert result["success"] is False  # pinned stale op voids the whole direct batch
        assert ENTRY_A in store.entries("memory")  # nothing applied
