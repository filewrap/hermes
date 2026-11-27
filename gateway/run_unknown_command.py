"""Unknown slash-command delivery: react, or send and self-delete.

The unknown-command reply used to travel back up to the adapter as a plain
string, which left a permanent "command doesn't exist" message in the chat
for every typo. The gateway now answers it itself on Telegram, governed by
the sender's personal Message Reactions setting (``reactions_enabled``,
default ON):
- ON: react 🤷 to the command message, send nothing.
- OFF, or the reaction fails for any reason: send the reply and
  delete it after 5 seconds (``telegram_self_delete_after_s`` metadata,
  handled by the adapter's self-delete scheduler).
Never react + send. Non-Telegram sources, a missing delivery adapter, or
any delivery failure fail open: the caller keeps the plain string.

A mistyped command with one clear close match gets a "did you mean"
suggestion instead of the bare notice (``suggest_command``). A suggestion
is always SENT, never just reacted to — a 🤷 would hide it — with the same
5s self-delete.
"""
from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING, Any, Optional

from agent.i18n import t
from gateway.config import Platform

if TYPE_CHECKING:
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

logger = logging.getLogger(__name__)

#: Seconds before a sent unknown-command reply deletes itself.
UNKNOWN_COMMAND_SELF_DELETE_S = 5

#: Minimum SequenceMatcher ratio for a "did you mean" suggestion. A typo
#: (transposition, one missing/extra/wrong letter) scores >= 0.75 against its
#: target ("/modle" -> "/model" is 0.8); unrelated words stay below 0.5
#: ("/xyzabc" best is 0.4). 0.7 admits short typos while rejecting junk.
_SUGGESTION_MIN_RATIO = 0.7

#: Chat types that count as a DM for scope filtering (same set as the
#: autonomous handler's ``_AUTONOMOUS_DM_CHAT_TYPES``).
_DM_CHAT_TYPES = frozenset({"dm", "private", "direct"})
#: Chat types that count as a group for scope filtering.
_GROUP_CHAT_TYPES = frozenset({"group", "forum", "thread"})

# Canonical commands whose handlers refuse by scope. The gateway has no
# declarative scope marker on CommandDef, so this set is the scope source
# for suggestions — each entry cites the enforcing handler. A command
# missing here is suggestible; the handler stays the enforcement point, so
# a stale entry can only ever produce a refused tap, never a privilege gain.
# Owner-only: autonomous (slash_commands_autonomous.py, owner + DM),
# draft (slash_commands_model.py _handle_draft_command, "owner-only,
# DM-only showcase"), login (slash_commands_login.py, admin-only,
# "Operator only"), sync (slash_commands.py _handle_sync_command,
# "Owner/admin-only"), bstop (b2b_bstop.py, "Only the configured owner can
# stop a talk."), allowgc and trigger (owner-only, group-only; allowgc.py,
# and a trigger "only the owner can set one").
_OWNER_ONLY_COMMANDS = frozenset(
    {"allowgc", "autonomous", "bstop", "draft", "login", "sync", "trigger"})
# DM-only (any admitted user, but only in a DM): autonomous, draft, login
# as above; topic (slash_commands_session.py, Telegram DM only).
_DM_ONLY_COMMANDS = frozenset({"autonomous", "draft", "login", "topic"})
# Group-only (meaningless outside a group): allowgc and trigger as above;
# imout ("Leave this group's shared profile") and whisper ("Send an
# ephemeral secret to one member of this group").
_GROUP_ONLY_COMMANDS = frozenset({"allowgc", "imout", "trigger", "whisper"})


def _scope_refuses(canonical: str, *, chat_type: str, is_owner: bool) -> bool:
    """True when *canonical* would be refused for this sender in this chat."""
    chat = str(chat_type or "").lower()
    if canonical in _OWNER_ONLY_COMMANDS and not is_owner:
        return True
    if canonical in _DM_ONLY_COMMANDS and chat not in _DM_CHAT_TYPES:
        return True
    if canonical in _GROUP_ONLY_COMMANDS and chat not in _GROUP_CHAT_TYPES:
        return True
    return False


def _suggestable_names() -> dict[str, str]:
    """Every matchable spelling (canonical name or alias) -> canonical name.

    Built from GATEWAY_KNOWN_COMMANDS, which covers the gateway dispatch
    lists (_IDLE_COMMANDS, _PLAIN_COMMANDS, _HM_CANONICAL_COMMANDS) — the
    registry is the single source they derive from. Unfiltered: scope is
    applied after matching (a refused best match suppresses the suggestion,
    it never re-ranks to a different command the user did not mean).
    """
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    spellings: dict[str, str] = {}
    for name in GATEWAY_KNOWN_COMMANDS:
        cmd = resolve_command(name)
        canonical = cmd.name if cmd is not None else str(name)
        spellings.setdefault(str(name), canonical)
        spellings.setdefault(canonical, canonical)
    return spellings


def suggest_command(command: str, *, chat_type: str = "", is_owner: bool = False) -> Optional[str]:
    """Canonical command a mistyped ``/command`` most likely meant, or None.

    Pure (in-memory registry only): the best SequenceMatcher ratio over the
    suggestable spellings must clear ``_SUGGESTION_MIN_RATIO`` and be
    strictly unique — a tie gets no suggestion. Aliases match too, but the
    suggestion is always the canonical name. Scope only suppresses: the best
    match is found first, and a best match the sender cannot run in this
    chat yields no suggestion (never a different command — "/topi" in a
    group means /topic, which is DM-only, not /stop).
    """
    typed = (command or "").strip().lower().lstrip("/").replace("_", "-")
    if not typed:
        return None
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS

    if typed in GATEWAY_KNOWN_COMMANDS:
        return None
    per_command: dict[str, float] = {}
    for spelling, canonical in _suggestable_names().items():
        ratio = difflib.SequenceMatcher(None, typed, spelling).ratio()
        if ratio > per_command.get(canonical, 0.0):
            per_command[canonical] = ratio
    ranked = sorted(per_command.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return None
    best, best_ratio = ranked[0]
    second_ratio = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_ratio < _SUGGESTION_MIN_RATIO or best_ratio <= second_ratio:
        return None
    if _scope_refuses(best, chat_type=chat_type, is_owner=is_owner):
        return None
    return best


def is_owner_sender(runner: Any, source: SessionSource) -> bool:
    """True when *source*'s sender is the owner, via the delivery adapter's
    owner check (same gate the autonomous handler uses). Fail closed: a
    missing adapter, a missing checker, or any error denies.
    """
    try:
        adapter = runner._delivery_adapter_for(source)
    except Exception:
        logger.debug("did-you-mean owner check: delivery adapter lookup failed", exc_info=True)
        return False
    if adapter is None:
        return False
    checker = getattr(adapter, "_is_owner_user", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(getattr(source, "user_id", None), source))
    except Exception:
        logger.debug("did-you-mean owner check failed; denying", exc_info=True)
        return False


def unknown_slash_reply(command: str, source: SessionSource, *, is_owner: bool = False) -> Optional[str]:
    """Reply for a /command that is not built-in/plugin/skill; None when it is known."""
    from gateway.run import _check_unavailable_skill
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
    # Known commands never need an unavailable-skill hint (which can require a cold scan).
    if command.replace("_", "-") in GATEWAY_KNOWN_COMMANDS:
        return None
    # Known-but-disabled or uninstalled skill → actionable guidance.
    _unavail_msg = _check_unavailable_skill(command)
    if _unavail_msg:
        return _unavail_msg
    # Genuinely unrecognized: warn instead of forwarding to the LLM as free text (it invents
    # tool calls). Normalize to hyphenated form first: the quick-command block may have set an
    # alias target, so the resolved def can be stale.
    logger.warning(
        "Unrecognized slash command /%s from %s — replying with unknown-command notice",
        command, source.platform.value if source.platform else "?",
    )
    suggestion = suggest_command(
        command, chat_type=getattr(source, "chat_type", ""), is_owner=is_owner)
    if suggestion is not None:
        return t(
            "gateway.unknown_command_suggestion", command=command, suggestion=suggestion)
    return t("gateway.unknown_command", command=command)


def _sender_reactions_enabled(adapter: Any, source: Any) -> bool:
    """The sender's single Message Reactions setting; True when unset or unreadable.

    The unknown-command 🤷 is a reaction to the sender's message, so the
    sender's own setting governs it — the same setting that gates every other
    reaction. Unreadable homes or non-numeric senders fail open to the default.
    """
    try:
        from gateway.telegram_personal_settings import message_reactions_enabled

        home = adapter._settings_home(source)
        return message_reactions_enabled(home, str(getattr(source, "user_id", "") or ""))
    except Exception:
        logger.debug("Message Reactions read failed; defaulting to on", exc_info=True)
        return True


async def maybe_deliver_unknown_command(
    runner: Any, event: MessageEvent, source: SessionSource,
    command: Optional[str], text: Optional[str],
) -> bool:
    """Deliver the unknown-command reply for Telegram, or leave it to the caller.

    Returns True when the reply was answered (reacted, or sent and scheduled
    to self-delete), so the caller returns None instead of the plain string.
    Returns False when the caller should keep the plain string (not the
    unknown-command reply, non-Telegram, no delivery adapter, delivery
    failed). Never raises.
    """
    try:
        return await _maybe_deliver_unknown_command(runner, event, source, command, text)
    except Exception:
        logger.debug("Telegram unknown-command delivery failed; keeping the plain reply", exc_info=True)
        return False


async def _maybe_deliver_unknown_command(
    runner: Any, event: MessageEvent, source: SessionSource,
    command: Optional[str], text: Optional[str],
) -> bool:
    if not command or text is None:
        return False
    # The genuine unknown-command reply is exactly one of the two locale
    # strings (plain, or with a "did you mean" suggestion); the
    # unavailable-skill hint shares the rewrite path but is actionable
    # guidance, so it keeps the old plain-string behaviour. Compare against
    # the locale text directly — re-deriving the reply would re-run the
    # disk-bound unavailable-skill scan a second time, on the loop. The
    # suggestion itself is recomputed here, but that match is pure
    # (in-memory registry, no disk scan).
    is_suggestion = False
    if text != t("gateway.unknown_command", command=command):
        suggestion = suggest_command(
            command, chat_type=getattr(source, "chat_type", ""),
            is_owner=is_owner_sender(runner, source))
        if suggestion is None or text != t(
                "gateway.unknown_command_suggestion",
                command=command, suggestion=suggestion):
            return False
        is_suggestion = True
    if getattr(source, "platform", None) is not Platform.TELEGRAM:
        return False
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return False
    from plugins.platforms.telegram.message_self_delete import SELF_DELETE_METADATA_KEY

    # A suggestion must be SEEN: skip the 🤷 react (which sends nothing)
    # and send the suggestion text instead, with the same 5s self-delete.
    if not is_suggestion and _sender_reactions_enabled(adapter, source):
        try:
            if await adapter.send_reaction(source.chat_id, event.message_id, "🤷"):
                return True
        except Exception:
            logger.debug("Telegram unknown-command reaction failed; falling back to send", exc_info=True)
    result = await adapter.send(
        source.chat_id, text, reply_to=event.message_id,
        metadata={SELF_DELETE_METADATA_KEY: UNKNOWN_COMMAND_SELF_DELETE_S})
    return bool(getattr(result, "success", False))


def busy_skill_rewrite(
    runner: Any, event: MessageEvent, source: SessionSource,
    session_key: str, command: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Disk-bound skill scan and load, off the loop (#111091)."""
    from agent.skill_bundles import resolve_bundle_command_key
    from agent.skill_commands import resolve_skill_command_key
    if resolve_bundle_command_key(command) is None and resolve_skill_command_key(command) is None:
        return False, None
    return True, runner._hm_skill_slash_rewrite(event, source, session_key, command)
