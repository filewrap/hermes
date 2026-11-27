"""Gateway /swarm command: registration, busy-path dispatch, and delivery.

Behaviour contracts only: the command is registered with a mid-run dispatch
policy, resolves through the handler table, lists live work as plain text on
non-Telegram platforms, and hands Telegram delivery to the swarm UI (returning
None once the card is sent, the model-UI precedent).
"""

from types import SimpleNamespace

import pytest

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command


def test_swarm_registered_with_mid_run_dispatch():
    cmd = resolve_command("swarm")
    assert cmd is not None
    assert cmd.name == "swarm"
    assert cmd.busy_policy == "dispatch"
    assert cmd.gateway_only is True


def test_swarm_in_plain_commands():
    from gateway.run_busy import GatewayBusySessionMixin
    assert "swarm" in GatewayBusySessionMixin._PLAIN_COMMANDS


def test_swarm_handler_resolves_by_name():
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    table = runner._command_handler_table(("swarm",))
    assert table["swarm"] == runner._handle_swarm_command


class _Event:
    def __init__(self, source, text="/swarm"):
        self.source = source
        self.text = text
        self.message_id = "7"

    def get_command_args(self):
        return self.text[len("/swarm"):].strip()


def _runner(home):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._resolve_profile_home_for_source = lambda source: home
    runner._session_key_for_source = lambda source: "agent:main:test:dm:1"
    return runner


@pytest.mark.asyncio
async def test_swarm_non_telegram_returns_plain_text_list(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    from gateway import swarm_sessions as ss
    ss.create_session(home, "12345", kind="kanban", source_id="t_root9",
                      goal="durable swarm", chat_id="42", chat_type="dm")
    runner = _runner(home)
    runner._delivery_adapter_for = lambda source: SimpleNamespace(platform="discord")
    source = SimpleNamespace(platform="discord", chat_id="42", user_id="12345",
                             user_name="Tester", chat_type="dm", thread_id=None,
                             profile=None)
    out = await runner._handle_swarm_command(_Event(source))
    assert isinstance(out, str)
    assert "durable swarm" in out


@pytest.mark.asyncio
async def test_swarm_non_telegram_empty_list_says_so(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runner = _runner(home)
    runner._delivery_adapter_for = lambda source: SimpleNamespace(platform="slack")
    source = SimpleNamespace(platform="slack", chat_id="42", user_id="12345",
                             user_name="Tester", chat_type="dm", thread_id=None,
                             profile=None)
    out = await runner._handle_swarm_command(_Event(source))
    assert isinstance(out, str)
    assert "No live agents" in out


@pytest.mark.asyncio
async def test_swarm_telegram_hands_off_to_ui_and_returns_none(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    sent = {}

    class FakeAdapter:
        platform = "telegram"
        _client = object()

        def _settings_ui(self):
            return SimpleNamespace(_home=lambda source: str(home))

    adapter = FakeAdapter()
    runner = _runner(home)
    runner._delivery_adapter_for = lambda source: adapter
    source = SimpleNamespace(platform="telegram", chat_id="42", user_id="12345",
                             user_name="Tester", chat_type="dm", thread_id=None,
                             profile=None)
    from plugins.platforms.telegram import swarm_ui
    real_handle = swarm_ui.TelegramSwarmUI.handle_command

    async def _spy(self, event, src, arguments, *, reply_to=None):
        sent["called"] = True
        return True

    monkeypatch.setattr(swarm_ui.TelegramSwarmUI, "handle_command", _spy)
    try:
        out = await runner._handle_swarm_command(_Event(source))
    finally:
        monkeypatch.setattr(swarm_ui.TelegramSwarmUI, "handle_command", real_handle)
    assert out is None
    assert sent.get("called") is True
