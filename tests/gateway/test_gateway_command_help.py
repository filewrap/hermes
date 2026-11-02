"""Gateway command help rendering tests."""

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _make_event(text: str, platform: Platform) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


@pytest.mark.asyncio
async def test_help_sanitizes_slash_command_mentions_for_telegram(monkeypatch):
    """Telegram help output must not expose invalid uppercase/hyphenated slashes."""
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {
            "/Linear": {"description": "Open Linear"},
            "/Custom-Thing": {"description": "Run a custom thing"},
        },
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/custom_thing`" in result
    assert "`/Linear`" not in result
    assert "`/Custom-Thing`" not in result
    assert "/commands" not in result


def test_public_help_registry_uses_help_and_model_plural_alias():
    from hermes_cli.commands import COMMAND_REGISTRY, GATEWAY_KNOWN_COMMANDS

    assert "commands" not in GATEWAY_KNOWN_COMMANDS
    assert "models" in GATEWAY_KNOWN_COMMANDS
    assert not any(command.name == "commands" for command in COMMAND_REGISTRY)


def test_clear_is_a_gateway_fresh_session_command_without_changing_new_reset_or_undo():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
    from gateway.run import GatewayRunner

    clear = resolve_command("clear")
    assert clear is not None and clear.cli_only is False
    assert clear.busy_policy == "interrupt_then_dispatch" and clear.busy_handler == "new"
    assert clear.desktop == "terminal"
    assert "clear" in GATEWAY_KNOWN_COMMANDS
    assert resolve_command("reset").name == "new"
    assert resolve_command("new").busy_handler == "new"
    assert resolve_command("undo").name == "undo"

    runner = object.__new__(GatewayRunner)
    reset_handler = lambda _event: "reset"
    runner._handle_reset_command = reset_handler
    assert runner._gateway_idle_command_handlers()["clear"] is reset_handler


@pytest.mark.asyncio
async def test_commands_is_unavailable_on_telegram():
    """Telegram no longer exposes /commands; /help is the supported command."""
    from gateway.run import GatewayRunner

    result = GatewayRunner._hm_unknown_slash_reply(
        "commands", _make_event("/commands 999", Platform.TELEGRAM).source
    )

    assert result is not None
    assert "Unknown command" in result
    # The guidance points to /help; `/commands` appears only as the echoed
    # command the user typed, never as a suggested command.
    assert "Type /help" in result
    assert "Type /commands" not in result


@pytest.mark.asyncio
async def test_help_skills_lists_all_skill_commands(monkeypatch):
    skill_commands = {
        f"/skill{index}": {"description": f"Skill {index}"} for index in range(12)
    }
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: skill_commands)

    result = await _make_runner()._handle_help_command(
        _make_event("/help skills", Platform.DISCORD)
    )

    assert "`/skill0`" in result
    assert "`/skill11`" in result


@pytest.mark.asyncio
async def test_help_overflow_points_to_supported_skill_listing(monkeypatch):
    skill_commands = {
        f"/skill{index:02d}": {"description": f"Skill {index}"} for index in range(12)
    }
    monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: skill_commands)

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.DISCORD)
    )

    assert "`/skill00`" in result
    assert "`/skill11`" not in result
    assert "/help skills" in result
    assert "/commands" not in result
