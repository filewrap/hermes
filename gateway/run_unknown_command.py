"""Unknown slash-command delivery: react, or send and self-delete.

The unknown-command reply used to travel back up to the adapter as a plain
string, which left a permanent "command doesn't exist" message in the chat
for every typo. The gateway now answers it itself on Telegram, governed by
the profile-wide ``platforms.telegram.react_on_unknown_commands`` setting
(read from the LAUNCH home, so the owner's DM toggle applies in groups
too — the gc profile has no settings UI):
- ON: react 🤷 to the command message, send nothing.
- OFF (default), or the reaction fails for any reason: send the reply and
  delete it after 5 seconds (``telegram_self_delete_after_s`` metadata,
  handled by the adapter's self-delete scheduler).
Never react + send. Non-Telegram sources, a missing delivery adapter, or
any delivery failure fail open: the caller keeps the plain string.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from agent.i18n import t
from gateway.config import Platform

if TYPE_CHECKING:
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

logger = logging.getLogger(__name__)

#: Seconds before a sent unknown-command reply deletes itself.
UNKNOWN_COMMAND_SELF_DELETE_S = 5


def unknown_slash_reply(command: str, source: SessionSource) -> Optional[str]:
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
    return t("gateway.unknown_command", command=command)


def react_on_unknown_commands_enabled(home: str | Path) -> bool:
    """Profile-wide ``platforms.telegram.react_on_unknown_commands``; False when unset or unreadable.

    Same read as the providers card (raw config.yaml, no merged-config
    dependency). The setting is owner-toggled from /settings; the gateway
    reads it from the LAUNCH home, never the session home.
    """
    try:
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(home) / "config.yaml")
        platforms = config.get("platforms") if isinstance(config, dict) else None
        telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
        if not isinstance(telegram, dict):
            return False
        return bool(telegram.get("react_on_unknown_commands", False))
    except Exception:
        logger.debug("react_on_unknown_commands read failed; defaulting to off", exc_info=True)
        return False


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
    # The genuine unknown-command reply is exactly the locale string; the
    # unavailable-skill hint shares the rewrite path but is actionable
    # guidance, so it keeps the old plain-string behaviour. Compare against
    # the locale text directly — re-deriving the reply would re-run the
    # disk-bound unavailable-skill scan a second time, on the loop.
    if text != t("gateway.unknown_command", command=command):
        return False
    if getattr(source, "platform", None) is not Platform.TELEGRAM:
        return False
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return False
    from hermes_constants import get_routing_process_hermes_home
    from plugins.platforms.telegram.message_self_delete import SELF_DELETE_METADATA_KEY

    if react_on_unknown_commands_enabled(get_routing_process_hermes_home()):
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
