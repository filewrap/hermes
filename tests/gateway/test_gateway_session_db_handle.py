"""Gateway turn code reads the session store through the Persistence handle.

Regression: the Nov 21 sync made the runner's ``_session_db`` a Persistence handle, but
``_sync_session_model_from_agent`` and ``_current_message_count`` still read the sync
SessionDB behind the pre-sync AsyncSessionDB forwarder (``._db``) — a path no live code
constructs anymore, so ``Persistence.__getattr__`` raised ``AttributeError`` and both
silently never ran (the Bot Chat provider backfill stayed pinned to stale providers;
the cross-process write-guard count always read None).
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from gateway.run_turn import GatewayTurnMixin
from gateway.run_turn_runner import TurnRunner


def _open_handle(tmpdir):
    from persistence_assembly import open_persistence, profile_scope

    return open_persistence(profile_scope(home=Path(tmpdir)))


def _seed_session(handle, session_id, model="old-model"):
    from persistence.conversation.sessions import NewSession

    handle.sessions.create(NewSession(session_id=session_id, source="test", model=model))


def test_sync_session_model_from_agent_writes_through_handle():
    with tempfile.TemporaryDirectory() as tmpdir:
        handle = _open_handle(tmpdir)
        try:
            _seed_session(handle, "sync-model-session")
            mixin = GatewayTurnMixin()
            mixin._session_db = handle
            agent = SimpleNamespace(
                model="new-model", provider="test", requested_provider=None,
                base_url=None, api_mode=None, _fallback_activated=False,
            )
            mixin._sync_session_model_from_agent("sync-model-session", agent)
            record = handle.sessions.get("sync-model-session")
            assert record.model == "new-model"
            assert record.model_config["gateway_runtime"]["provider"] == "test"
        finally:
            handle.close()


def test_current_message_count_reads_through_handle():
    with tempfile.TemporaryDirectory() as tmpdir:
        from persistence.conversation.messages import NewMessage

        handle = _open_handle(tmpdir)
        try:
            _seed_session(handle, "count-session")
            handle.messages.append("count-session", NewMessage(role="user", content="hi"))
            runner = object.__new__(TurnRunner)
            runner._ctx = SimpleNamespace(session_id="count-session")
            runner._runner = SimpleNamespace(_session_db=handle)
            assert runner._current_message_count() == 1
        finally:
            handle.close()
