"""Gateway contract and live dispatch for /approvals."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import hermes_yaml as yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _event(text: str = "/approvals") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="user-1",
            chat_id="chat-1",
            chat_type="dm",
        ),
    )


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace(platforms={})
    runner.hooks = MagicMock(loaded_hooks=[])
    runner.hooks.emit = AsyncMock(return_value=[])
    runner._running_agents = {}
    runner._get_or_create_gateway_honcho = lambda _key: (None, None)
    runner._is_user_authorized = lambda _source: True
    runner.session_store = SimpleNamespace(get_or_create_session=lambda _source: None)
    return runner


@pytest.mark.asyncio
async def test_gateway_rejects_non_admin_persistent_approval_change():
    runner = _runner()
    runner.config = SimpleNamespace(
        platforms={
            Platform.TELEGRAM: SimpleNamespace(
                extra={
                    "allow_admin_from": ["admin-1"],
                    "user_allowed_commands": ["approvals"],
                }
            )
        }
    )

    with patch("hermes_cli.approval_mode.run_approval_mode_command") as run:
        output = await runner._handle_approvals_command(_event("/approvals off"))

    assert "admin" in output.lower()
    run.assert_not_called()




def test_approval_mode_save_failure_hides_the_exception(monkeypatch):
    """A config-write failure must not leak the raw exception into the reply."""
    from hermes_cli import approval_mode

    def _boom(*args, **kwargs):
        raise RuntimeError("boom: /secret/config.yaml unparseable")

    monkeypatch.setattr("hermes_cli.config.set_config_value", _boom)

    result = approval_mode.run_approval_mode_command("off")
    assert result.ok is False
    assert "boom" not in result.message
    assert "/secret/config.yaml" not in result.message
    assert "Could not save" in result.message
