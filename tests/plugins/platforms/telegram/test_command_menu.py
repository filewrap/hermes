from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.platforms.telegram.command_menu import (
    register_default_command_menu,
    register_personal_command_menu,
)


class _Command:
    def __init__(self, command, description):
        self.command = command
        self.description = description


class _ScopeDefault:
    pass


class _ScopePeer:
    def __init__(self, peer):
        self.peer = peer


class _ScopePeerUser:
    def __init__(self, peer, user_id):
        self.peer, self.user_id = peer, user_id


class _Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _telethon_modules(monkeypatch):
    types = SimpleNamespace(
        BotCommand=_Command,
        BotCommandScopeDefault=_ScopeDefault,
        BotCommandScopePeer=_ScopePeer,
        BotCommandScopePeerUser=_ScopePeerUser,
    )
    functions = SimpleNamespace(bots=SimpleNamespace(
        SetBotCommandsRequest=_Request,
        GetBotCommandsRequest=_Request,
    ))
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(
        types=types, functions=functions,
        utils=SimpleNamespace(get_input_user=lambda peer: f"user:{peer}")))


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.rows = []

    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, _Request) and hasattr(request, "commands"):
            self.rows = request.commands
            return True
        return self.rows

    async def get_input_entity(self, user_id):
        return f"peer:{user_id}"


class BotCommandsTooMuchError(Exception):
    pass


class _LimitedClient(_FakeClient):
    def __init__(self, max_commands):
        super().__init__()
        self.max_commands = max_commands

    async def __call__(self, request):
        if hasattr(request, "commands") and len(request.commands) > self.max_commands:
            self.calls.append(request)
            raise BotCommandsTooMuchError("BOT_COMMANDS_TOO_MUCH")
        return await super().__call__(request)


@pytest.mark.asyncio
async def test_default_menu_uses_native_set_get_without_scanning_private_skills(monkeypatch):
    _telethon_modules(monkeypatch)
    client = _FakeClient()
    with (
        patch("hermes_cli.commands_platforms.telegram_bot_commands",
              return_value=[("help", "Show help"), ("extra_one", "One"), ("extra_two", "Two")]),
        patch("hermes_cli.commands_platforms.telegram_menu_max_commands", return_value=1),
        patch("hermes_cli.commands_platforms.telegram_menu_commands",
              side_effect=AssertionError("Public scope must not scan private skills")),
    ):
        result = await register_default_command_menu(client)

    assert result.verified is True
    assert result.scope == "default"
    assert result.registered_count == 1
    assert result.overflow_count == 2
    assert result.overflow_names == ("extra_one", "extra_two")
    assert isinstance(client.calls[0].scope, _ScopeDefault)
    assert client.calls[0].commands[0].command == "help"
    assert isinstance(client.calls[1], _Request)
    assert client.calls[1].scope is client.calls[0].scope


@pytest.mark.asyncio
async def test_personal_menu_uses_private_peer_scope_and_only_explicit_commands(monkeypatch):
    _telethon_modules(monkeypatch)
    client = _FakeClient()
    core = [("help", "Show help"), ("status", "Show status")]
    all_commands = [*core, ("plug", "Plugin command")]
    with patch("hermes_cli.commands_platforms.telegram_bot_commands",
               side_effect=lambda *, include_plugins=True: all_commands if include_plugins else core):
        result = await register_personal_command_menu(
            client, 4242, [("my_skill", "Personal skill")])

    assert result.verified is True
    assert result.scope == "user:4242"
    assert result.registered_count == 4
    assert isinstance(client.calls[0].scope, _ScopePeer)
    assert client.calls[0].scope.peer == "peer:4242"
    assert [row.command for row in client.calls[0].commands] == [
        "help", "status", "plug", "my_skill"]


@pytest.mark.asyncio
async def test_personal_menu_limits_at_100_and_reports_all_overflow(monkeypatch):
    _telethon_modules(monkeypatch)
    client = _FakeClient()
    commands = [(f"skill_{i}", f"Skill {i}") for i in range(103)]

    with patch("hermes_cli.commands_platforms.telegram_bot_commands", return_value=[]):
        result = await register_personal_command_menu(client, "9", commands)

    assert result.registered_count == 100
    assert result.overflow_count == 3
    assert result.overflow_names == ("skill_100", "skill_101", "skill_102")
    assert len(client.calls[0].commands) == 100


@pytest.mark.asyncio
async def test_personal_menu_adapts_to_lower_mtproto_limit_and_reports_private_overflow(monkeypatch):
    _telethon_modules(monkeypatch)
    client = _LimitedClient(max_commands=4)
    core = [("help", "Show help"), ("status", "Show status")]
    personal = [(f"skill_{i}", f"Skill {i}") for i in range(6)]
    with patch("hermes_cli.commands_platforms.telegram_bot_commands",
               side_effect=lambda *, include_plugins=True: core):
        result = await register_personal_command_menu(client, "4242", personal)

    set_requests = [request for request in client.calls if hasattr(request, "commands")]
    assert result.verified
    assert result.scope == "user:4242"
    assert result.registered_count == 4
    assert result.overflow_names == ("skill_2", "skill_3", "skill_4", "skill_5")
    assert [row.command for row in client.rows] == ["help", "status", "skill_0", "skill_1"]
    assert len(set_requests[0].commands) > 4
    assert any(len(request.commands) == 4 for request in set_requests)
    assert all(isinstance(request.scope, _ScopePeer) for request in set_requests)
    assert all(request.scope.peer == "peer:4242" for request in set_requests)


@pytest.mark.asyncio
async def test_group_skill_menu_is_scoped_to_the_member_not_whole_group(monkeypatch):
    _telethon_modules(monkeypatch)
    client = _FakeClient()
    with patch("hermes_cli.commands_platforms.telegram_bot_commands", return_value=[("help", "Help")]):
        result = await register_personal_command_menu(
            client, "42", [("private_skill", "Only this member")], chat_id="-1009")
    scope = client.calls[0].scope
    assert result.verified
    assert isinstance(scope, _ScopePeerUser)
    assert scope.peer == "peer:-1009"
    assert scope.user_id == "user:peer:42"
    assert result.scope == "chat:-1009:user:42"
