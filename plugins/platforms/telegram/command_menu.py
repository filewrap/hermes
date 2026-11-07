"""Native MTProto registration for Telegram bot slash-command menus.

Default commands are shared commands only. Personal skill names must be composed for
one resolved user/profile and registered with :func:`register_personal_command_menu`,
which uses that user's private-chat scope; never merge a per-user catalog into the
default scope.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
TELEGRAM_MAX_BOT_COMMANDS = 100


@dataclass(frozen=True)
class CommandMenuResult:
    """Outcome and overflow metadata from one SetBotCommands/GetBotCommands cycle."""

    scope: str
    registered_count: int
    overflow_count: int
    overflow_names: tuple[str, ...]
    verified: bool


def _bounded_commands(
    commands: Sequence[tuple[str, str]], max_commands: int,
) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
    """Deduplicate and validate supplied menu rows, preserving caller priority."""
    limit = max(1, min(TELEGRAM_MAX_BOT_COMMANDS, int(max_commands)))
    valid: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_description in commands:
        name = str(raw_name).strip()
        description = str(raw_description).strip() or "Command"
        if (not name or len(name) > 32 or
                any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in name)):
            logger.warning("Skipping invalid Telegram BotCommand name %r", raw_name)
            continue
        if len(description) > 256:
            description = description[:253] + "..."
        if name in seen:
            continue
        seen.add(name)
        valid.append((name, description))
    return valid[:limit], tuple(name for name, _desc in valid[limit:])


def _is_too_many_commands_error(exc: Exception) -> bool:
    """Recognize Telegram's MTProto rejection without swallowing unrelated RPC errors."""
    error_name = type(exc).__name__.lower().replace("_", "")
    error_text = str(exc).upper()
    return "botcommandstoomuch" in error_name or "BOT_COMMANDS_TOO_MUCH" in error_text


async def _set_and_verify(
    client: Any,
    commands: Sequence[tuple[str, str]],
    *,
    scope: Any,
    scope_name: str,
    lang_code: str = "",
    max_commands: int = TELEGRAM_MAX_BOT_COMMANDS,
) -> CommandMenuResult:
    """Set the largest accepted prefix, then verify it with GetBotCommands.

    Keep the existing conservative 100-command menu bound. The MTProto method
    reference does not specify a separate count. If it returns a too-many-command RPC
    error, probe down only for that error; unrelated failures remain visible.
    Built-in-first input order keeps the highest-priority commands on overflow.
    """
    from telethon import functions, types

    limit = max(1, min(TELEGRAM_MAX_BOT_COMMANDS, int(max_commands)))
    candidates, local_overflow = _bounded_commands(commands, TELEGRAM_MAX_BOT_COMMANDS)
    requested = candidates[:limit]
    if requested:
        best_count = 0
        high = len(requested)
        probe_count = len(requested)
        last_too_many: Exception | None = None
        while probe_count > best_count:
            bot_commands = [types.BotCommand(command=name, description=desc)
                            for name, desc in requested[:probe_count]]
            try:
                accepted = await client(functions.bots.SetBotCommandsRequest(
                    scope=scope, lang_code=lang_code, commands=bot_commands))
            except Exception as exc:
                if not _is_too_many_commands_error(exc):
                    raise
                last_too_many = exc
                high = probe_count - 1
                if high <= best_count:
                    break
                probe_count = best_count + (high - best_count + 1) // 2
                continue
            if accepted is False:
                raise RuntimeError(f"Telegram rejected command menu for {scope_name} scope")
            best_count = probe_count
            if best_count >= high:
                break
            probe_count = best_count + (high - best_count + 1) // 2
        if best_count == 0:
            assert last_too_many is not None
            raise last_too_many
        bounded = requested[:best_count]
    else:
        bounded = []
        accepted = await client(functions.bots.SetBotCommandsRequest(
            scope=scope, lang_code=lang_code, commands=[]))
        if accepted is False:
            raise RuntimeError(f"Telegram rejected command menu for {scope_name} scope")

    overflow = (tuple(name for name, _desc in candidates[len(bounded):])
                + local_overflow)
    actual = await client(functions.bots.GetBotCommandsRequest(
        scope=scope, lang_code=lang_code))
    expected_rows = [(name, desc) for name, desc in bounded]
    actual_rows = [(str(getattr(row, "command", "")),
                    str(getattr(row, "description", ""))) for row in actual]
    verified = actual_rows == expected_rows
    if not verified:
        logger.error(
            "Telegram command menu verification failed for %s scope: expected %d commands, got %d",
            scope_name, len(expected_rows), len(actual_rows))
    return CommandMenuResult(scope_name, len(bounded), len(overflow), overflow, verified)


async def register_default_command_menu(
    client: Any, *, max_commands: int | None = None, lang_code: str = "",
) -> CommandMenuResult:
    """Register shared built-ins, plugins and globally shared skills in default scope.

    Personal/profile-specific skill names are intentionally not discovered here. Callers
    must pass those names through the private-chat helper after resolving the user's
    effective Hermes profile.
    """
    from telethon import types
    from hermes_cli.commands_platforms import (
        _prioritize_telegram_menu_candidates, telegram_bot_commands, telegram_menu_max_commands)

    limit = telegram_menu_max_commands() if max_commands is None else max_commands
    # The default scope is visible to every user. Profile/private skills must
    # only enter the explicitly user-bound scope below, never a global scan.
    commands = await asyncio.to_thread(telegram_bot_commands, include_plugins=True)
    # Rank the core rows directly: routing through telegram_menu_commands would
    # scan private skills into this global scope. Ranked commands survive the
    # 100-command cap; the deterministic tail (button, webauth) overflows instead
    # of whichever core commands happen to sit last in registry order.
    ranked = _prioritize_telegram_menu_candidates(
        [(name, desc, "core", name) for name, desc in commands])
    commands = [(name, desc) for name, desc, _source, _raw in ranked]
    return await _set_and_verify(
        client, commands, scope=types.BotCommandScopeDefault(), scope_name="default",
        lang_code=lang_code, max_commands=limit)


async def register_personal_command_menu(
    client: Any,
    user_id: int | str,
    personal_skill_commands: Sequence[tuple[str, str]],
    *,
    shared_commands: Sequence[tuple[str, str]] = (),
    chat_id: int | str | None = None,
    lang_code: str = "",
    max_commands: int = TELEGRAM_MAX_BOT_COMMANDS,
) -> CommandMenuResult:
    """Register a built-in-first menu for one user's private-chat scope.

    ``personal_skill_commands`` must come only from this user's resolved profile. The
    helper prepends all actual registered built-ins (plus adapter slash commands), then
    optional explicitly shared dynamic rows, then personal skills. It never scans the
    process-global skill cache, preventing one user's personal names from being published
    to everyone. ``shared_commands`` may be used for skill names known to be genuinely
    global; do not pass a profile-specific catalog there.
    """
    from telethon import types
    from hermes_cli.commands_platforms import (
        _normalize_telegram_desc, _sanitize_telegram_name,
        _truncate_desc, telegram_bot_commands)

    def normalize_rows(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        normalized = []
        for raw_name, description in rows:
            name = _sanitize_telegram_name(str(raw_name).lstrip("/"))
            if name:
                normalized.append((name, _truncate_desc(
                    _normalize_telegram_desc(str(description)), 256)))
        return normalized

    core = telegram_bot_commands(include_plugins=False)
    all_native = telegram_bot_commands(include_plugins=True)
    core_names = {name for name, _desc in core}
    plugin_rows = [(name, desc) for name, desc in all_native if name not in core_names]
    commands = [*core, *plugin_rows, *normalize_rows(shared_commands),
                *normalize_rows(personal_skill_commands)]
    peer = await client.get_input_entity(int(user_id))
    if chat_id is not None and str(chat_id) != str(user_id):
        from telethon import utils
        scope = types.BotCommandScopePeerUser(
            peer=await client.get_input_entity(int(chat_id)), user_id=utils.get_input_user(peer))
        scope_name = f"chat:{int(chat_id)}:user:{int(user_id)}"
    else:
        scope = types.BotCommandScopePeer(peer=peer)
        scope_name = f"user:{int(user_id)}"
    return await _set_and_verify(
        client, commands, scope=scope,
        scope_name=scope_name, lang_code=lang_code,
        max_commands=max_commands)
