"""Tests for hermes_cli/wakeups.py — agent-scheduled one-shot session wakeups (schedule_wakeup tool)."""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def test_resolve_due_at_clamps_delay_and_rejects_past_when():
    from hermes_cli.wakeups import MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, resolve_due_at

    now = 1_000_000.0
    due, err, note = resolve_due_at(delay="3", now=now)
    assert err is None and due == now + MIN_DELAY_SECONDS and "minimum" in note
    due, err, note = resolve_due_at(delay="30d", now=now)
    assert err is None and due == now + MAX_DELAY_SECONDS and "7-day" in note
    assert resolve_due_at(delay="1h30m", now=now)[0] == now + 5400
    assert resolve_due_at(when="2020-01-01T00:00:00Z", now=time.time())[1] == "`when` is not in the future"
    assert resolve_due_at(now=now)[1]  # neither given
    assert resolve_due_at(delay="5m", when="2030-01-01T00:00:00Z", now=now)[1]  # both given


def test_due_prompt_claims_once_and_abandon_rearms(hermes_home):
    """A due wakeup is removed from the persisted set BEFORE its turn runs (no double fire across
    pollers); a dispatch that never started a turn puts it back so it fires on the next idle poll."""
    from hermes_cli.wakeups import MAX_PER_SESSION, WakeupManager

    mgr = WakeupManager("wk-sid")
    now = time.time()
    wakeup, err, _ = mgr.schedule("re-check CI", delay="10s", reason="ci", now=now - 60)
    assert err is None and wakeup is not None
    later, _, _ = mgr.schedule("far future", delay="1d", now=now)
    assert WakeupManager("wk-sid").load().pending[0].id == wakeup.id  # sorted by due time

    prompt = mgr.due_prompt(now)
    assert prompt and "re-check CI" in prompt and wakeup.id in prompt and "ci" in prompt
    assert WakeupManager("wk-sid").due_prompt(now) is None  # claimed — a second poller sees nothing due
    assert [w.id for w in mgr.load().pending] == [later.id]

    assert mgr.abandon_fire() is True
    assert [w.id for w in mgr.load().pending] == [wakeup.id, later.id]
    assert mgr.abandon_fire() is False  # a claim re-arms exactly once

    assert mgr.cancel(wakeup.id) is True and mgr.cancel(wakeup.id) is False
    for _ in range(MAX_PER_SESSION - 1):
        assert mgr.schedule("x", delay="1h")[1] is None
    assert "maximum" in mgr.schedule("one too many", delay="1h")[1]


def test_wakeup_persists_across_managers_and_lists_sessions(hermes_home):
    """Save/load/list round-trip through the real Persistence store handle: a wakeup
    scheduled by one manager is visible to a fresh manager, list_sessions_with_wakeups
    finds the session, and store_has_pending_wakeup agrees. Regression for the
    get_meta breakage — the old SessionDB calls raised AttributeError on the handle."""
    from hermes_cli import goals
    from hermes_cli.wakeups import (
        WakeupManager,
        list_sessions_with_wakeups,
        store_has_pending_wakeup,
    )

    db = goals._get_session_db()
    assert db is not None
    assert store_has_pending_wakeup(db) is False

    now = time.time()
    mgr = WakeupManager("list-sid")
    wakeup, err, _ = mgr.schedule("ping me", delay="10s", now=now - 60)
    assert err is None and wakeup is not None
    other = WakeupManager("other-sid")
    later, err, _ = other.schedule("later", delay="1d", now=now)
    assert err is None and later is not None

    # Fresh managers see the persisted rows.
    assert [w.id for w in WakeupManager("list-sid").load().pending] == [wakeup.id]
    assert [w.id for w in WakeupManager("other-sid").load().pending] == [later.id]
    assert set(list_sessions_with_wakeups()) == {"list-sid", "other-sid"}
    assert store_has_pending_wakeup(db) is True

    # Clearing one session drops it from the listing; clearing both empties it.
    assert WakeupManager("list-sid").clear() == 1
    assert list_sessions_with_wakeups() == ["other-sid"]
    assert WakeupManager("other-sid").clear() == 1
    assert list_sessions_with_wakeups() == []
    assert store_has_pending_wakeup(db) is False


def test_claim_due_returns_wakeup_and_rearms(hermes_home):
    """claim_due() is the gateway's handle on a due wakeup: it claims (removes) the earliest
    due wakeup and returns the Wakeup itself, so the admission identity can carry the wakeup
    id; abandon_fire() re-arms it. The authority lane never asked WakeupManager for anything,
    so due wakeups loaded fine and fired never — this is the API that lane needs."""
    from hermes_cli.wakeups import WakeupManager

    mgr = WakeupManager("claim-sid")
    now = time.time()
    wakeup, err, _ = mgr.schedule("re-check CI", delay="10s", reason="ci", now=now - 60)
    assert err is None and wakeup is not None
    later, _, _ = mgr.schedule("far future", delay="1d", now=now)

    claimed = mgr.claim_due(now)
    assert claimed is not None and claimed.id == wakeup.id and claimed.prompt == "re-check CI"
    assert WakeupManager("claim-sid").claim_due(now) is None  # claimed — a second poller sees nothing due
    assert [w.id for w in mgr.load().pending] == [later.id]

    assert mgr.abandon_fire() is True
    assert [w.id for w in mgr.load().pending] == [wakeup.id, later.id]
    assert mgr.claim_due(now).id == wakeup.id
