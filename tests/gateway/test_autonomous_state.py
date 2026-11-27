"""Behaviour contracts for the autonomous-mode run store + state machine.

Covers ``gateway/autonomous_state.py``: the file-backed active flag, the
run records, the explicit transitions, atomic checkpointing, and the boot
reset. No behaviour is wired yet — these tests pin the store the runner
will call.
"""
from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from gateway import autonomous_state as store
from hermes_cli.config_defaults import DEFAULT_CONFIG


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    """A fake launch home, installed as the routing process home."""
    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


class _Adapter:
    """Minimal stand-in for the Telegram adapter's setting surface."""

    def __init__(self, *, runtime=None, extra=None):
        self._autonomous_mode_runtime = runtime
        self.config = SimpleNamespace(extra=extra or {})

    @staticmethod
    def _truthy_setting(value, default=False):
        return bool(value) if value is not None else default


def test_setting_keys_registered_with_spec_defaults():
    """The master switch defaults off; the badges default on."""
    telegram = DEFAULT_CONFIG["telegram"]
    assert telegram["autonomous_mode"] is False
    assert telegram["autonomous_badges"] is True


def test_create_run_starts_idle_with_ledger_defaults(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Tidy the inbox", model="deepseek-v4")
    assert record["state"] == "idle"
    assert record["goal"] == "Tidy the inbox"
    assert record["session_key"] == "agent:main"
    assert record["model"] == "deepseek-v4"
    assert record["steps"] == 0
    assert record["input_tokens"] == 0 and record["output_tokens"] == 0
    assert record["stall_count"] == 0
    assert record["last_tool"] is None and record["last_error"] is None
    assert record["stopped_by"] is None
    assert record["started_at"] and record["last_step_at"]

    path = home / "autonomous" / "runs" / f"{record['run_id']}.json"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert store.load_run(home, record["run_id"]) == record


def test_valid_transition_chain(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Tidy the inbox")
    run_id = record["run_id"]

    started = store.start_run(home, run_id)
    assert started["state"] == "running"
    assert store.is_active(home) is True
    assert store.load_state(home)["current_run_id"] == run_id

    waiting = store.mark_needs_input(home, run_id, last_tool="terminal")
    assert waiting["state"] == "needs_input"
    assert waiting["last_tool"] == "terminal"

    resumed = store.resume_run(home, run_id)
    assert resumed["state"] == "running"
    assert store.is_active(home) is True

    done = store.mark_done(home, run_id)
    assert done["state"] == "done"
    assert store.load_run(home, run_id)["state"] == "done"


def test_running_to_stopped_and_failed_are_terminal(tmp_path):
    home = tmp_path / "home"
    stopped = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, stopped["run_id"])
    record = store.mark_stopped(home, stopped["run_id"], stopped_by="owner")
    assert record["state"] == "stopped"
    assert record["stopped_by"] == "owner"
    with pytest.raises(ValueError):
        store.transition(home, stopped["run_id"], "running")

    failed = store.create_run(home, "agent:main", "Goal B")
    store.start_run(home, failed["run_id"])
    record = store.mark_failed(home, failed["run_id"], last_error="provider down")
    assert record["state"] == "failed"
    assert record["last_error"] == "provider down"
    with pytest.raises(ValueError):
        store.transition(home, failed["run_id"], "needs_input")


def test_invalid_transitions_raise_and_change_nothing(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal")
    run_id = record["run_id"]

    for target in ("done", "stopped", "failed", "needs_input"):
        with pytest.raises(ValueError):
            store.transition(home, run_id, target)
    with pytest.raises(ValueError):
        store.transition(home, run_id, "teleporting")
    with pytest.raises(ValueError):
        store.transition(home, "run_nope", "running")
    with pytest.raises(ValueError):
        store.transition(home, run_id, "running", frobnicate=True)
    assert store.load_run(home, run_id)["state"] == "idle"

    store.start_run(home, run_id)
    with pytest.raises(ValueError):
        store.transition(home, run_id, "idle")
    with pytest.raises(ValueError):
        store.transition(home, run_id, "running")
    # needs_input -> done is not an edge: the agent must resume first.
    store.mark_needs_input(home, run_id)
    with pytest.raises(ValueError):
        store.transition(home, run_id, "done")
    assert store.load_run(home, run_id)["state"] == "needs_input"


def test_checkpoint_round_trip_after_each_tool_round(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal")
    store.start_run(home, record["run_id"])

    saved = store.checkpoint(home, record["run_id"], steps=3, input_tokens=1200,
                             output_tokens=400, last_tool="read_file", stall_count=1)
    assert saved["steps"] == 3
    assert saved["input_tokens"] == 1200 and saved["output_tokens"] == 400
    assert saved["last_tool"] == "read_file"
    assert saved["stall_count"] == 1
    assert saved["state"] == "running"
    assert saved["last_step_at"]
    assert store.load_run(home, record["run_id"]) == saved

    with pytest.raises(ValueError):
        store.checkpoint(home, record["run_id"], state="done")
    with pytest.raises(ValueError):
        store.checkpoint(home, "run_nope", steps=1)


def test_active_flag_is_runtime_state_not_config(tmp_path):
    home = tmp_path / "home"
    assert store.is_active(home) is False
    store.set_active(home, True, run_id="run_abc")
    assert store.is_active(home) is True
    assert store.load_state(home)["current_run_id"] == "run_abc"
    # The live flag never lands in config.yaml.
    assert not (home / "config.yaml").exists()

    store.set_active(home, False)
    assert store.is_active(home) is False
    assert store.load_state(home)["current_run_id"] is None


def test_boot_marking_keeps_flag_pin_and_run_id(tmp_path):
    home = tmp_path / "home"
    store.set_active(home, True, run_id="run_abc",
                     pinned_message_id=123, chat_id=456)
    assert store.is_active(home) is True

    # No run record to fail: nothing changes, and a second boot agrees.
    assert store.fail_interrupted_run_on_boot(home) is None
    assert store.is_active(home) is True
    state = store.load_state(home)
    assert state["pinned_message_id"] == 123 and state["chat_id"] == 456
    assert state["current_run_id"] == "run_abc"
    assert store.fail_interrupted_run_on_boot(home) is None


def test_corrupt_state_reads_as_inactive(tmp_path):
    home = tmp_path / "home"
    state_path = home / "autonomous" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json", encoding="utf-8")
    assert store.load_state(home) == {"active": False, "current_run_id": None,
                                      "pinned_message_id": None, "chat_id": None,
                                      "closed_note": None}
    assert store.is_active(home) is False


def test_run_id_traversal_is_refused(tmp_path):
    home = tmp_path / "home"
    assert store.load_run(home, "../state") is None
    assert store.load_run(home, "run/x") is None
    with pytest.raises(ValueError):
        store.checkpoint(home, "../../etc", steps=1)


def test_boot_marking_marks_interrupted_run_failed_and_keeps_flag(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, record["run_id"])
    store.set_active(home, True, run_id=record["run_id"],
                     pinned_message_id=123, chat_id=456)

    failed = store.fail_interrupted_run_on_boot(home)

    assert failed is not None and failed["run_id"] == record["run_id"]
    assert failed["state"] == "failed"
    assert failed["last_error"] == "gateway restarted mid-run"
    assert store.is_active(home) is True
    state = store.load_state(home)
    assert state["pinned_message_id"] == 123 and state["chat_id"] == 456
    assert state["current_run_id"] == record["run_id"]
    # The failed record is terminal now: a second boot marks nothing.
    assert store.fail_interrupted_run_on_boot(home) is None


def test_boot_marking_leaves_a_waiting_run_waiting(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, record["run_id"])
    store.mark_needs_input(home, record["run_id"])
    store.set_active(home, True, run_id=record["run_id"])

    assert store.fail_interrupted_run_on_boot(home) is None
    assert store.is_active(home) is True
    assert store.load_run(home, record["run_id"])["state"] == "needs_input"


def test_boot_marking_marks_done_when_last_tool_was_autonomous_done(tmp_path):
    """A run whose agent already called autonomous_done is done, not failed:
    the done transition raced the restart, but the run's own ledger says
    the agent signaled the goal complete — the boot marker must not
    contradict it."""
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, record["run_id"])
    store.checkpoint(home, record["run_id"], steps=150, last_tool="autonomous_done")
    store.set_active(home, True, run_id=record["run_id"])

    marked = store.fail_interrupted_run_on_boot(home)

    assert marked is not None and marked["run_id"] == record["run_id"]
    assert marked["state"] == "done"
    assert not marked.get("last_error")
    assert store.load_run(home, record["run_id"])["state"] == "done"


def test_boot_marking_heals_failed_run_whose_last_tool_was_autonomous_done(tmp_path):
    """A record an earlier boot already stamped failed with last tool
    autonomous_done is repaired to done on the next boot: the state must
    match what actually happened, and the stale restart error goes."""
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, record["run_id"])
    store.checkpoint(home, record["run_id"], steps=150, last_tool="autonomous_done")
    store.mark_failed(home, record["run_id"], last_error="gateway restarted mid-run")
    store.set_active(home, False)

    assert store.fail_interrupted_run_on_boot(home) is None

    healed = store.load_run(home, record["run_id"])
    assert healed["state"] == "done"
    assert not healed.get("last_error")


def test_boot_marking_leaves_other_failed_runs_failed(tmp_path):
    """The heal is narrow: a failed run whose last tool was anything else
    keeps its failed state and its error."""
    home = tmp_path / "home"
    record = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, record["run_id"])
    store.checkpoint(home, record["run_id"], steps=3, last_tool="shell")
    store.mark_failed(home, record["run_id"], last_error="gateway restarted mid-run")
    store.set_active(home, False)

    assert store.fail_interrupted_run_on_boot(home) is None

    kept = store.load_run(home, record["run_id"])
    assert kept["state"] == "failed"
    assert kept["last_error"] == "gateway restarted mid-run"


def test_delete_run_removes_the_record_and_refuses_the_live_run(tmp_path):
    home = tmp_path / "home"
    old = store.create_run(home, "agent:main", "Old goal")
    store.start_run(home, old["run_id"])
    store.mark_stopped(home, old["run_id"])
    live = store.create_run(home, "agent:main", "Live goal")
    store.start_run(home, live["run_id"])

    assert store.delete_run(home, old["run_id"]) is True
    assert store.load_run(home, old["run_id"]) is None
    assert store.delete_run(home, old["run_id"]) is False

    with pytest.raises(ValueError):
        store.delete_run(home, live["run_id"])
    assert store.load_run(home, live["run_id"]) is not None


def test_find_run_for_session_returns_latest_for_that_session(tmp_path):
    home = tmp_path / "home"
    first = store.create_run(home, "agent:main:telegram:dm:1", "Goal A")
    store.start_run(home, first["run_id"])
    other = store.create_run(home, "agent:main:telegram:dm:2", "Goal B")
    store.start_run(home, other["run_id"])
    second = store.create_run(home, "agent:main:telegram:dm:1", "Goal C")
    store.start_run(home, second["run_id"])

    assert store.find_run_for_session(home, "agent:main:telegram:dm:1")["run_id"] == second["run_id"]
    assert store.find_run_for_session(home, "agent:main:telegram:dm:2")["run_id"] == other["run_id"]
    assert store.find_run_for_session(home, "agent:main:telegram:dm:9") is None


def test_reopen_run_resumes_terminal_runs_but_not_idle_or_running(tmp_path):
    home = tmp_path / "home"

    stopped = store.create_run(home, "agent:main", "Goal A")
    store.start_run(home, stopped["run_id"])
    store.mark_stopped(home, stopped["run_id"], stopped_by="owner")
    reopened = store.reopen_run(home, stopped["run_id"])
    assert reopened["state"] == "running"
    assert store.is_active(home) is True

    failed = store.create_run(home, "agent:main", "Goal B")
    store.start_run(home, failed["run_id"])
    store.mark_failed(home, failed["run_id"], last_error="boom")
    assert store.reopen_run(home, failed["run_id"])["state"] == "running"

    waiting = store.create_run(home, "agent:main", "Goal C")
    store.start_run(home, waiting["run_id"])
    store.mark_needs_input(home, waiting["run_id"])
    assert store.reopen_run(home, waiting["run_id"])["state"] == "running"

    # A done run re-opens as a continuation of its goal — Resume has no
    # age or state limit except killed; only idle/running/unknown refuse.
    done = store.create_run(home, "agent:main", "Goal D")
    store.start_run(home, done["run_id"])
    store.mark_done(home, done["run_id"])
    assert store.reopen_run(home, done["run_id"])["state"] == "running"

    killed = store.create_run(home, "agent:main", "Goal G")
    store.start_run(home, killed["run_id"])
    store.mark_failed(home, killed["run_id"], last_error="boom")
    store.mark_killed(home, killed["run_id"])
    with pytest.raises(ValueError):
        store.reopen_run(home, killed["run_id"])

    idle = store.create_run(home, "agent:main", "Goal E")
    with pytest.raises(ValueError):
        store.reopen_run(home, idle["run_id"])

    running = store.create_run(home, "agent:main", "Goal F")
    store.start_run(home, running["run_id"])
    with pytest.raises(ValueError):
        store.reopen_run(home, running["run_id"])

    with pytest.raises(ValueError):
        store.reopen_run(home, "run_nope")


def test_compact_tokens_wraps_big_totals(tmp_path):
    """Token totals read compact — 1.2M, 3.4B — never a bare long number."""
    assert store.compact_tokens(0) == "0"
    assert store.compact_tokens(999) == "999"
    assert store.compact_tokens(1000) == "1.0K"
    assert store.compact_tokens(4203) == "4.2K"
    assert store.compact_tokens(1_234_567) == "1.2M"
    assert store.compact_tokens(3_400_000_000) == "3.4B"


def test_run_budget_cap_reads_configured_value(tmp_path):
    """The run budget is the configured cap, never a hardcoded 1M."""
    home = tmp_path / "home"
    home.mkdir()
    assert store.run_budget_cap(home) == 1_000_000

    (home / "config.yaml").write_text(
        "autonomous:\n  max_tokens_per_run: 250000\n", encoding="utf-8")
    assert store.run_budget_cap(home) == 250000

    (home / "config.yaml").write_text(
        "autonomous:\n  max_tokens_per_run: 0\n", encoding="utf-8")
    assert store.run_budget_cap(home) == 0

    (home / "config.yaml").write_text(
        "autonomous:\n  max_tokens_per_run: banana\n", encoding="utf-8")
    assert store.run_budget_cap(home) == 1_000_000


def test_find_resumable_run_offers_a_done_run(tmp_path):
    """Resume has no age or state limit except killed: the newest
    resumable run may be a finished one."""
    home = tmp_path / "home"

    done = store.create_run(home, "agent:main", "Goal D")
    store.start_run(home, done["run_id"])
    store.mark_done(home, done["run_id"])
    store.set_active(home, False)

    found = store.find_resumable_run(home)
    assert found is not None and found["run_id"] == done["run_id"]

    store.mark_killed(home, done["run_id"])
    assert store.find_resumable_run(home) is None


def test_autonomous_mode_enabled_runtime_override_wins(launch_home):
    (launch_home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    autonomous_mode: true\n", encoding="utf-8")
    assert store.autonomous_mode_enabled(_Adapter(runtime=False)) is False
    assert store.autonomous_mode_enabled(_Adapter(runtime=True)) is True


def test_autonomous_mode_enabled_extra_then_launch_home(launch_home):
    assert store.autonomous_mode_enabled(_Adapter(extra={"autonomous_mode": True})) is True
    assert store.autonomous_mode_enabled(_Adapter(extra={"autonomous_mode": False})) is False

    (launch_home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    autonomous_mode: true\n", encoding="utf-8")
    assert store.autonomous_mode_enabled(_Adapter()) is True
    assert store.autonomous_mode_enabled(None) is True

    (launch_home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    autonomous_mode: false\n", encoding="utf-8")
    assert store.autonomous_mode_enabled(_Adapter()) is False

    (launch_home / "config.yaml").unlink()
    assert store.autonomous_mode_enabled(_Adapter()) is False
