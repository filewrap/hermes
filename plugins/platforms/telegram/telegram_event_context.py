"""Telegram event-text / channel-prompt / observe-attribution helpers (Telethon form).

Pure functions mirroring upstream's PTB-shaped ``telegram_context`` /
``TelegramAdapter`` behaviour, so the Telethon adapter stays a thin caller and
the 4k-line adapter file does not grow new logic. Upstream references are the
``v0.22.7`` tag: ``plugins/platforms/telegram/telegram_context.py`` and
``plugins/platforms/telegram/adapter.py``.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from gateway.platforms._shared import decode_json_list_literal, get_scoped_secret
from gateway.platforms.base import resolve_channel_prompt
from gateway.platforms.event import MessageType

_GROUP_CHAT_TYPES = frozenset({"group", "forum"})


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _secret(name: str, default: str = "") -> str:
    return str(get_scoped_secret(name, default) or "").strip()


def _as_str_set(value: Any) -> set[str]:
    value = decode_json_list_literal(value)
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    return {str(part).strip() for part in (value or []) if str(part).strip()}


def clean_bot_trigger_text(text: str, bot_username: str | None) -> str:
    """Strip our own ``@handle`` from event text.

    Mirror of upstream's ``TelegramAdapter._clean_bot_trigger_text``: remove a
    case-insensitive ``@<bot_username>`` plus any trailing ``[:, -]`` and
    whitespace, then strip. The original text is returned when the result is
    empty (a message that IS only the mention keeps it, e.g. a code span).
    """
    username = str(bot_username or "").strip().lstrip("@")
    if not text or not username:
        return text
    cleaned = re.sub(rf"(?i)@{re.escape(username)}\b[,:\\-]*\s*", "", text).strip()
    return cleaned or text


def mentions_other_participants(messages: list[Any], bot_username: str | None,
                                bot_id: Any) -> bool:
    """True when a ``mention``/``text_mention`` entity names someone other than this bot.

    Telethon form of upstream's ``telegram_context.mentions_other_participants``:
    a ``MessageEntityMention`` span is read from its message's text; a
    ``MessageEntityMentionName``/``MessageEntityInputMentionName`` names the bot
    only when its user id is the bot's. Other entity types (bot_command, code,
    ...) are not mentions.
    """
    username = str(bot_username or "").strip().lstrip("@").lower()
    for message in messages or []:
        text = str(getattr(message, "message", "") or "")
        for entity in (getattr(message, "entities", None) or []):
            kind = type(entity).__name__
            if kind in {"MessageEntityMentionName", "MessageEntityInputMentionName"}:
                if bot_id is None:
                    return True
                target = getattr(entity, "user_id", None)
                target_id = getattr(target, "user_id", target)
                try:
                    if int(target_id) != int(bot_id):
                        return True
                except (TypeError, ValueError):
                    return True
            elif kind == "MessageEntityMention":
                try:
                    offset = int(getattr(entity, "offset", 0))
                    length = int(getattr(entity, "length", 0))
                    handle = text[offset:offset + length].strip().lstrip("@").lower()
                except (TypeError, ValueError):
                    continue
                if handle and handle != username:
                    return True
    return False


def group_trigger_text(text: str, chat_type: str | None, messages: list[Any],
                       bot_username: str | None, bot_id: Any) -> str:
    """Event text with our own handle stripped when we are the sole addressee.

    Mirror of upstream's ``telegram_context.group_trigger_text``: with other
    participants named the text stays verbatim (``@research_bot , @ops_bot …``
    must not reach us as ``, @ops_bot …``); otherwise the bot mention (and a
    command's ``@bot`` suffix) is cleaned out of the text the model sees.
    """
    if chat_type in _GROUP_CHAT_TYPES and mentions_other_participants(
            messages, bot_username, bot_id):
        return text
    return clean_bot_trigger_text(text, bot_username)


def resolve_telegram_channel_prompt(config_extra: dict, chat_id: str,
                                    thread_id: str | None) -> str | None:
    """Per-chat ephemeral prompt for this chat/topic (upstream parity).

    Mirror of upstream's ``_build_message_event`` lookup: the thread id wins,
    else the chat id; the chat id is the parent when a thread is present.
    Blank prompts count as absent (see ``gateway.platforms.base``).
    """
    return resolve_channel_prompt(
        config_extra or {}, thread_id or chat_id, chat_id if thread_id else None)


def telegram_event_channel_prompt(config_extra: dict, source: Any,
                                  identity_prompt: str | None) -> str | None:
    """Channel prompt for the event: per-chat prompt merged with the identity.

    Mirror of upstream's ``_build_message_event`` + ``group_identity_prompt``:
    in a group the resolved per-chat prompt comes first, then the identity
    block (either alone when the other is absent). Outside a group the event
    carries the resolved per-chat prompt as-is — empty in a DM with no
    per-chat prompt, so the identity prompt never leaks into DMs.
    """
    resolved = resolve_telegram_channel_prompt(
        config_extra, str(getattr(source, "chat_id", "") or ""),
        getattr(source, "thread_id", None))
    if getattr(source, "chat_type", None) in _GROUP_CHAT_TYPES:
        if resolved and identity_prompt:
            return f"{resolved}\n\n{identity_prompt}"
        return resolved or identity_prompt
    return resolved


def observe_unmentioned_group_messages(config_extra: dict) -> bool:
    """Store skipped unmentioned group messages as context (upstream parity).

    Mirror of upstream's ``_telegram_observe_unmentioned_group_messages``:
    ``config.extra`` wins, then the env var, default off.
    """
    extra = config_extra or {}
    if "observe_unmentioned_group_messages" in extra:
        return _truthy(extra.get("observe_unmentioned_group_messages"), default=False)
    raw = _secret("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES")
    if raw:
        return _truthy(raw, default=False)
    return False


def observe_allowed_chats(config_extra: dict) -> set[str]:
    """Chats where observed group context may use a shared source (upstream parity).

    ``group_allowed_chats`` ∩ ``allowed_chats`` (when set); empty when
    ``group_allowed_chats`` is empty.
    """
    extra = config_extra or {}
    group_allowed = _as_str_set(
        extra.get("group_allowed_chats") or _secret("TELEGRAM_GROUP_ALLOWED_CHATS"))
    if not group_allowed:
        return set()
    response_allowed = _as_str_set(
        extra.get("allowed_chats") or _secret("TELEGRAM_ALLOWED_CHATS"))
    return group_allowed & response_allowed if response_allowed else group_allowed


def group_observe_channel_prompt(bot_username: str | None, bot_id: Any) -> str:
    """Marker block for observed Telegram group context (upstream parity)."""
    username = str(bot_username or "").strip().lstrip("@") or "unknown"
    return (
        "You are handling a Telegram group chat message.\n"
        f"- Your identity: user_id={bot_id or 'unknown'}, @-mention name in this group=@{username}\n"
        "- observed Telegram group context may be provided in a separate context-only block "
        "before the current message; it is not necessarily addressed to you.\n"
        "- Treat only the current new message as a request explicitly directed at you, "
        "and use observed context only when the current message asks for it.")


def group_observe_shared_source(source: Any) -> Any:
    """Chat/topic-scoped source for observed Telegram group context (upstream parity)."""
    return dataclasses.replace(source, user_id=None, user_name=None, user_id_alt=None)


def group_observe_attributed_text(event: Any) -> str:
    """Observed text attributed to its sender (upstream parity)."""
    user_id = getattr(event.source, "user_id", None) or "unknown"
    return f"[{getattr(event.source, 'user_name', None) or user_id}|{user_id}]\n{event.text or ''}"


def apply_group_observe_attribution(event: Any, *, observe_enabled: bool,
                                    allowed_chats: set[str], bot_username: str | None,
                                    bot_id: Any) -> Any:
    """Align triggered group turns with observed-history attribution.

    Mirror of upstream's ``_apply_telegram_group_observe_attribution``: when
    observe mode is on and this group chat is in the observe-allowed set, the
    observed-context marker is appended to the channel prompt; a non-command
    event also gets its text attributed to the sender and its source replaced
    with the shared (anonymised) source. Commands keep the original source so
    slash-access checks can identify the sender.
    """
    if not observe_enabled:
        return event
    source = getattr(event, "source", None)
    if getattr(source, "chat_type", None) not in _GROUP_CHAT_TYPES:
        return event
    allowed = set(allowed_chats or set())
    if not allowed or str(getattr(source, "chat_id", "") or "") not in allowed:
        return event
    observe_prompt = group_observe_channel_prompt(bot_username, bot_id)
    channel_prompt = (f"{event.channel_prompt}\n\n{observe_prompt}"
                      if getattr(event, "channel_prompt", None) else observe_prompt)
    if getattr(event, "message_type", None) == MessageType.COMMAND:
        return dataclasses.replace(event, channel_prompt=channel_prompt)
    return dataclasses.replace(
        event, text=group_observe_attributed_text(event),
        source=group_observe_shared_source(source), channel_prompt=channel_prompt)
