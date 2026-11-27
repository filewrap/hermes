"""Storage-interface wiring for agents built on a Persistence handle.

Regression: the Nov 21 sync handed gateway agents the Persistence handle itself as
``session_db`` (``gateway/run.py`` ``_session_db``), but ``stand_in_interface`` only
recognised a ``hermes_state.SessionDB`` and returned None for the handle, so
``agent.persistence`` stayed None and every dispatch site fell back to old SessionDB
methods on the handle — ``AttributeError: flush_token_counts`` / ``get_session`` /
``create_session`` / ``append_messages_batch`` on the first live wakeup turn
(2026-11-22 12:28:50 UTC, session 20261119_022732_c297e68e).
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.agent_init_session import agent_interface, stand_in_interface


def _open_handle(tmpdir):
    from persistence_assembly import open_persistence, profile_scope

    return open_persistence(profile_scope(home=Path(tmpdir)))


def _make_gateway_style_agent(handle, session_id):
    """An agent built the way the gateway builds one: ``session_db`` is the handle,
    no ``persistence`` kwarg (``gateway/run_turn_runner.py``)."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        return AIAgent(
            api_key="<redacted>",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=handle,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )


def test_stand_in_interface_returns_persistence_handle():
    with tempfile.TemporaryDirectory() as tmpdir:
        handle = _open_handle(tmpdir)
        try:
            assert stand_in_interface(handle) is handle
            assert stand_in_interface(None) is None
        finally:
            handle.close()


def test_stand_in_interface_rejects_read_only_handle():
    with tempfile.TemporaryDirectory() as tmpdir:
        from persistence_assembly import open_persistence_reader

        writer = _open_handle(tmpdir)
        writer.close()
        with open_persistence_reader(Path(tmpdir)) as reader:
            assert reader.read_only
            assert stand_in_interface(reader) is None


def test_gateway_style_agent_persists_session_row_through_handle():
    with tempfile.TemporaryDirectory() as tmpdir:
        handle = _open_handle(tmpdir)
        try:
            agent = _make_gateway_style_agent(handle, "gateway-style-session")
            try:
                assert agent.persistence is handle
                assert agent_interface(agent) is handle
                agent._ensure_db_session()
                row = handle.sessions.get("gateway-style-session")
                assert row is not None
                assert row.session_id == "gateway-style-session"
            finally:
                agent.close()
        finally:
            handle.close()
