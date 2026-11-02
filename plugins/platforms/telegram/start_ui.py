"""The /start command's native welcome; presentation never changes access.

The welcome is sent as native Rich HTML (``InputRichMessageHTML``): the
sender's profile action is an in-body ``<tg-button>`` linking to
``tg://user?id=...``, exactly the pill buttons clients render for rich
messages. Hand-built ``PageBlock`` payloads are not used here: the server
rejects them for these cards with ``RICH_MESSAGE_BLOCK_UNSUPPORTED``.
"""
from __future__ import annotations

import re
from html import escape

from telethon import types

from .mtproto_rich import rich_button, rich_button_row


TAGLINE = "A little clarity, a fresh idea, a useful next step — that's my kind of conversation."
CAPABILITIES = (
    "Answer questions and research topics worth exploring.",
    "Help with writing, editing, and coding.",
    "Untangle ideas and turn tasks into clear next steps.",
)
HELP = "Send /help for a quick guide, or bring me something you'd like to work through."
UNRECOGNIZED = "I don’t recognize you in this profile. Ask the owner for access."
OWNER_ACCESS = "Ask to be added to this profile, then return here once access is granted."


def _numeric_id(value: object, *, what: str) -> str:
    text = str(value or "").strip()
    if not text.isdecimal():
        raise ValueError(f"{what} requires a numeric Telegram user ID")
    return text


def _greeting_inline(user_id: object, user_name: str | None) -> str:
    """Greet by display name with the sender's profile action inline."""
    label = " ".join(str(user_name or "").split()) or "you"
    button = rich_button(label, kind="profile", value=_numeric_id(user_id, what="/start user"))
    return f"hey {button}!"


def _identity_inline(bot_name: str, bot_id: object | None) -> str:
    """Identity opener; the bot's name is its own profile button when its id is known."""
    name = " ".join(str(bot_name or "Naina").split()) or "Naina"
    try:
        button = rich_button(name, kind="profile", value=_numeric_id(bot_id, what="/start bot"))
    except ValueError:
        return f"I'm {escape(name)}, an AI assistant."
    return f"I'm {button}, an AI assistant."


def _capabilities_line() -> str:
    """The three capabilities as one quiet line, not a list."""
    return " · ".join(text.rstrip(".") for text in CAPABILITIES) + "."


def start_html_view(*, bot_name: str, allowed: bool, user_id: object,
                    user_name: str | None = None,
                    owner_id: object | None = None,
                    bot_id: object | None = None) -> str:
    """Build the /start welcome as Rich HTML with an in-body profile button.

    Only numeric IDs are needed: the profile action is a ``tg://user?id=``
    button, so no resolved ``InputUser`` entity is required. Unlisted
    senders keep the nested owner-contact action; presentation never
    changes access. Names and the bot name are data, never markup.
    """
    greeting = _greeting_inline(user_id, user_name)
    if not allowed:
        owner = _numeric_id(owner_id, what="Unlisted /start owner")
        contact = rich_button_row([
            rich_button("Contact owner", kind="profile", value=owner, style="primary"),
        ])
        return (
            f"<p>{greeting}</p>"
            + f"<p>{escape(UNRECOGNIZED)}</p>"
            + "<details><summary>Want to meet me?</summary>"
            + f"<p>{escape(OWNER_ACCESS)}</p>"
            + f"{contact}</details>"
        )
    identity = _identity_inline(bot_name, bot_id)
    help_text = escape(HELP).replace("/help", "<code>/help</code>", 1)
    return (
        f"<p>{greeting} {identity}</p>"
        + f"<p><i>{escape(TAGLINE)}</i></p>"
        + f"<p>{escape(_capabilities_line())}</p>"
        + f"<p>{help_text}</p>"
    )


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _rich_text(value, entities: list, offset: int = 0) -> str:
    """Flatten RichText nodes for the required companion message field."""
    if isinstance(value, str):
        return value
    if isinstance(value, types.TextEmpty):
        return ""
    if isinstance(value, types.TextConcat):
        parts = []
        cursor = offset
        for item in value.texts:
            text = _rich_text(item, entities, cursor)
            parts.append(text)
            cursor += _utf16_length(text)
        return "".join(parts)
    if isinstance(value, (types.TextBold, types.TextUrl)):
        text = _rich_text(value.text, entities, offset)
        length = _utf16_length(text)
        if length:
            if isinstance(value, types.TextBold):
                entities.append(types.MessageEntityBold(offset=offset, length=length))
            else:
                entities.append(types.MessageEntityTextUrl(offset=offset, length=length, url=value.url))
        return text
    return _rich_text(value.text, entities, offset)


def _block_companion_text(block: object) -> str:
    """Create the required SendMessage companion field without retrying as plain text."""
    if isinstance(block, types.PageBlockButtonRow):
        return " ".join(_rich_text(button.text, []) for button in block.buttons)
    if isinstance(block, types.PageBlockList):
        return "\n".join(f"• {_rich_text(item.text, [])}" for item in block.items)
    if isinstance(block, types.PageBlockDetails):
        parts = [_rich_text(block.title, [])]
        parts.extend(_block_companion_text(item) for item in block.blocks)
        return "\n".join(part for part in parts if part)
    value = getattr(block, "text", None)
    return _rich_text(value, []) if value is not None else ""


def start_companion_text(welcome: types.InputRichMessage) -> str:
    """Build the mandatory ordinary message field accompanying the native rich payload."""
    return "\n\n".join(part for part in (_block_companion_text(block) for block in welcome.blocks) if part)


def safe_start_error(exc: Exception) -> str:
    """Log the RPC symbol, never str(exc), which may include the whole request."""
    reason = getattr(exc, "message", None)
    if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z_]{2,95}", reason):
        return f"{type(exc).__name__}: {reason}"
    return type(exc).__name__
