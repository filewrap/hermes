"""Change how a Telegram group invokes the bot, in the owner's words.

The only writer besides the /trigger command and the settings UI: the agent
tool that lets the owner switch invocation (mention / trigger word / every
message) and set the trigger word from plain words. It writes the SAME store
the /trigger command writes — the profile's config.yaml
``platforms.telegram.chat_access.groups.<chat_id>`` record — through
``plugins/platforms/telegram/telegram_chat_settings.update_chat_settings``,
so the adapter's ``_group_triggered`` reads the change on the next message.
Group trigger words stay per-group; a change in one group never touches
another. Mention is the bot-wide behaviour: the @username works everywhere
by nature, and ``tag`` mode is what selects it for this group.

Owner-only in the same three layers as the other owner-gated tools: tenant
sessions never see it (it is not in ``agent/tenant_policy.SUPPORTED``), the
Telegram adapter hides it from every non-launch-owner sender via
``disabled_tools_for_source``, and this handler re-checks the owner itself.
Direct mode mirrors the settings UI's two-step confirm: it sends every new
message in the group to Hermes, so it needs ``confirm_direct`` set
explicitly — the owner's words alone do not enable it.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from gateway.config import Platform
from gateway.session_context import get_session_env
from tools.gateway_owner import requester_is_owner
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TELEGRAM_TRIGGER_TOOL_NAME = "telegram_trigger"
_MODES = ("trigger", "tag", "direct")

TELEGRAM_TRIGGER_SCHEMA = {
    "name": TELEGRAM_TRIGGER_TOOL_NAME,
    "description": (
        "Change how this Telegram group invokes the bot. Owner-only. "
        "Modes: trigger (bot name, trigger word, or a reply invokes it), "
        "tag (an @username mention or a reply invokes it), direct (every "
        "message invokes it; needs confirm_direct). Set trigger_word to a "
        "single word, or empty to turn the word off. With no mode or word, "
        "just report the current settings for this group."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "description": "Invocation mode for this group: trigger, tag, or direct.",
            },
            "trigger_word": {
                "type": "string",
                "description": "Single trigger word for this group; empty string turns the word off.",
            },
            "confirm_direct": {
                "type": "boolean",
                "description": "Required to enable direct mode: it sends every new message in this group to Hermes.",
            },
        },
        "additionalProperties": False,
    },
}


def telegram_trigger(args: dict[str, Any], **kwargs: Any) -> str:
    """Write the group's invocation settings, or report them when asked nothing."""
    if not requester_is_owner(kwargs.get("parent_agent")):
        return tool_error("Only the owner can change how the bot is invoked.")
    if (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower() != Platform.TELEGRAM.value:
        return tool_error("Invocation triggers are a Telegram group setting.")
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    chat_type = (get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "").strip().lower()
    if not chat_id:
        return tool_error(
            "No Telegram chat in this turn; say this in the group whose triggers you want to change.")
    if chat_type not in {"group", "forum"}:
        return tool_error(
            "Trigger settings are per-group. Say this in the group whose triggers you want to change.")
    from hermes_constants import get_hermes_home
    from plugins.platforms.telegram.telegram_chat_settings import (
        load_chat_settings,
        update_chat_settings,
    )

    home = get_hermes_home()
    mode = args.get("mode")
    word = args.get("trigger_word")
    if mode is None and word is None:
        settings = load_chat_settings(home, chat_type, chat_id)
        return json.dumps({
            "chat_id": chat_id,
            "trigger_mode": settings["trigger_mode"],
            "trigger_word": settings["trigger_word"],
        }, ensure_ascii=False)
    changes: dict[str, Any] = {}
    if mode is not None:
        mode = str(mode).strip().lower()
        if mode not in _MODES:
            return tool_error("Mode must be trigger, tag, or direct.")
        if mode == "direct" and args.get("confirm_direct") is not True:
            return tool_error(
                "Direct mode sends every new message in this group to Hermes. "
                "Confirm it explicitly to enable it.")
        changes["trigger_mode"] = mode
    if word is not None:
        if not isinstance(word, str):
            return tool_error("The trigger word must be a single word.")
        changes["trigger_word"] = word.strip()
    try:
        settings = update_chat_settings(home, chat_type, chat_id, changes)
    except ValueError as exc:
        return tool_error(str(exc) or "Could not save this group setting.")
    return json.dumps({
        "chat_id": chat_id,
        "trigger_mode": settings["trigger_mode"],
        "trigger_word": settings["trigger_word"],
        "changed": sorted(changes),
        "note": "Takes effect on the next message in this group.",
    }, ensure_ascii=False)


registry.register(
    name=TELEGRAM_TRIGGER_TOOL_NAME,
    toolset="telegram_admin",
    schema=TELEGRAM_TRIGGER_SCHEMA,
    handler=telegram_trigger,
)
