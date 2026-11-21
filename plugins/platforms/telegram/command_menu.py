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
    ephemeral_verified: bool = True


# Commands delivered as ephemeral commands: the client sends them only to
# the bot, as UpdateNewEphemeralMessage, and they never appear in the chat.
EPHEMERAL_MENU_COMMANDS: frozenset[str] = frozenset({"imout", "whisper"})


def _flag_ephemeral(
    commands: Sequence[tuple[str, str]],
) -> list[tuple[str, str, bool]]:
    """Carry the ephemeral flag on menu rows (default False)."""
    return [(name, desc, name in EPHEMERAL_MENU_COMMANDS)
            for name, desc in commands]


def _bounded_commands(
    commands: Sequence[tuple[str, str] | tuple[str, str, bool]], max_commands: int,
) -> tuple[list[tuple[str, str, bool]], tuple[str, ...]]:
    """Deduplicate and validate supplied menu rows, preserving caller priority."""
    limit = max(1, min(TELEGRAM_MAX_BOT_COMMANDS, int(max_commands)))
    valid: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for row in commands:
        raw_name, raw_description = row[0], row[1]
        ephemeral = bool(row[2]) if len(row) > 2 else False
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
        valid.append((name, description, ephemeral))
    return valid[:limit], tuple(name for name, _desc, _eph in valid[limit:])


def _is_too_many_commands_error(exc: Exception) -> bool:
    """Recognize Telegram's MTProto rejection without swallowing unrelated RPC errors."""
    error_name = type(exc).__name__.lower().replace("_", "")
    error_text = str(exc).upper()
    return "botcommandstoomuch" in error_name or "BOT_COMMANDS_TOO_MUCH" in error_text


async def _set_and_verify(
    client: Any,
    commands: Sequence[tuple[str, str] | tuple[str, str, bool]],
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

    The ephemeral flag is verified explicitly, not dropped: when the readback
    echoes it, a mismatch fails verification; when the server omits the flag
    entirely on readback, the name/description verification still stands and
    ``ephemeral_verified`` reports False with a warning, since a missing echo
    cannot distinguish "not ephemeral" from "server does not report it".
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
            bot_commands = [
                types.BotCommand(command=name, description=desc, ephemeral=True)
                if ephemeral else types.BotCommand(command=name, description=desc)
                for name, desc, ephemeral in requested[:probe_count]]
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

    overflow = (tuple(name for name, _desc, _eph in candidates[len(bounded):])
                + local_overflow)
    actual = await client(functions.bots.GetBotCommandsRequest(
        scope=scope, lang_code=lang_code))
    expected_rows = [(name, desc, ephemeral) for name, desc, ephemeral in bounded]
    actual_rows = [(str(getattr(row, "command", "")),
                    str(getattr(row, "description", "")),
                    getattr(row, "ephemeral", None)) for row in actual]
    name_desc_verified = (
        [(name, desc) for name, desc, _eph in expected_rows]
        == [(name, desc) for name, desc, _eph in actual_rows])
    verified = name_desc_verified
    ephemeral_expected = any(ephemeral for _name, _desc, ephemeral in expected_rows)
    ephemeral_verified = True
    if ephemeral_expected:
        # An explicit False on readback is an echo; only None (flag omitted
        # entirely) means the server does not report the flag.
        echo_present = any(ephemeral is not None for _n, _d, ephemeral in actual_rows)
        if echo_present:
            ephemeral_verified = (
                len(actual_rows) == len(expected_rows)
                and all((actual_eph is True) == expected_eph
                        for (_n, _d, expected_eph), (_n2, _d2, actual_eph)
                        in zip(expected_rows, actual_rows)))
            if not ephemeral_verified:
                verified = False
                logger.error(
                    "Telegram command menu ephemeral verification failed for %s scope: "
                    "expected ephemeral flags %s, got %s",
                    scope_name,
                    [(name, eph) for name, _desc, eph in expected_rows if eph],
                    [(name, eph) for name, _desc, eph in actual_rows],
                )
        else:
            ephemeral_verified = False
            logger.warning(
                "Telegram command menu for %s scope: server did not echo the "
                "ephemeral flag on readback, so the ephemeral flag could not "
                "be verified (name/description verification still stands)",
                scope_name)
    if not name_desc_verified:
        logger.error(
            "Telegram command menu verification failed for %s scope: expected %d commands, got %d",
            scope_name, len(expected_rows), len(actual_rows))
    return CommandMenuResult(scope_name, len(bounded), len(overflow), overflow,
                             verified, ephemeral_verified)


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
    # 100-command cap; the deterministic tail (insights, sync, version, button,
    # webauth) overflows instead of whichever core commands happen to sit last
    # in registry order.
    ranked = _prioritize_telegram_menu_candidates(
        [(name, desc, "core", name) for name, desc in commands])
    commands = _flag_ephemeral(
        [(name, desc) for name, desc, _source, _raw in ranked])
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
    commands = _flag_ephemeral([*core, *plugin_rows, *normalize_rows(shared_commands),
                                *normalize_rows(personal_skill_commands)])
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
