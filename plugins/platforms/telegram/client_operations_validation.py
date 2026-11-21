"""Pure validators for telegram_client payloads.

Moved out of ``client_operations.py`` (which sits at the 2,000-line
code-health ratchet) — these take no adapter state, so they live here as
plain functions and are called directly from ``_validate_payload``.
"""
from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urlsplit

_RECIPIENT_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")

# Emoji base-glyph ranges: code points that can head a single emoji.
_EMOJI_BASE_RANGES = (
    (0x1F000, 0x1FAFF),  # emoticons, pictographs, transport, supplemental symbols
    (0x2600, 0x27BF),  # misc symbols, dingbats
    (0x2194, 0x21FF),  # arrows
    (0x2B00, 0x2BFF),  # misc symbols and arrows
    (0x2300, 0x23FF),  # misc technical
)
# Code points that may only follow a base glyph: VS16, ZWJ, keycap, skin tones.
_EMOJI_MODIFIERS = frozenset(
    {0xFE0F, 0x200D, 0x20E3, *range(0x1F3FB, 0x1F400)}
)


def validate_recipient(value: Any) -> str:
    """Normalize a send target: numeric Telegram ID or @username, nothing else.

    A numeric ID may be a user, group, supergroup, or channel ID. A username
    must be a valid Telegram username shape (5-32 characters, starts with a
    letter); anything else — URLs, deep links, paths, display names — is
    refused here, and resolution against Telegram happens at send time.
    """
    if isinstance(value, bool):
        raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")
    if isinstance(value, int):
        if value == 0 or abs(value) > 2**63 - 1:
            raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        digits = text.removeprefix("-")
        if digits.isdecimal() and len(digits) <= 19:
            number = int(text)
            if number == 0 or abs(number) > 2**63 - 1:
                raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")
            return str(number)
        if _RECIPIENT_USERNAME_RE.match(text):
            return "@" + text.lstrip("@")
    raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")


def validate_emoji(value: Any) -> str:
    """Accept exactly one real emoji (one base glyph plus modifiers).

    Rejects words, typos, multi-emoji strings, and the U+FFFD replacement
    character — the old check (any single non-ASCII char) let those through
    to the server, which answered with a cryptic REACTION_INVALID. A
    structurally valid emoji outside Telegram's reaction set (e.g. 🎵)
    still passes this check and is refused by the server instead; see the
    ReactionInvalidError mapping in ``_execute_mutation``.
    """
    if not isinstance(value, str):
        raise ValueError("emoji must be a single emoji, e.g. 👍")
    emoji = value.strip()
    if not emoji or len(emoji) > 8:
        raise ValueError("emoji must be a single emoji, e.g. 👍; a word or typo is not a reaction")
    bases = [char for char in emoji
             if ord(char) not in _EMOJI_MODIFIERS
             and any(low <= ord(char) <= high for low, high in _EMOJI_BASE_RANGES)]
    if (len(bases) != 1
            or any(ord(char) not in _EMOJI_MODIFIERS and char not in bases for char in emoji)
            or any(char.isspace() or char.isalnum() for char in emoji)):
        raise ValueError("emoji must be a single emoji, e.g. 👍; a word or typo is not a reaction")
    return emoji
_SEND_MESSAGE_KEYS = frozenset(
    {
        "recipient",
        "content",
        "format",
        "reply_to",
        "thread_id",
        "silent",
        "buttons",
        "quote",
        "effect",
        "pin",
        "pin_silent",
        "media",
        "filename",
        "content_type",
        "as_voice",
        "video_note",
        "force_document",
        "as_document",
        "location",
    }
)

_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.DOTALL)


def _strict_bool(p: dict[str, Any], key: str, default: bool = False) -> bool:
    value = p.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _validate_send_media(p: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one attachment: an http(s) URL, a base64/data-URL payload, or nothing.

    Local paths are never accepted here: telegram_client params carry no paths.
    """
    raw = p.get("media")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("media must be an http(s) URL or a base64/data-URL attachment")
    text = raw.strip()
    mime: str | None = None
    if text.startswith("data:"):
        match = _DATA_URL_RE.match(text)
        if not match or match.group(2) != ";base64":
            raise ValueError("media data-URLs must carry a base64 payload")
        try:
            base64.b64decode(match.group(3), validate=True)
        except Exception as exc:
            raise ValueError("media data-URL is not valid base64") from exc
        mime = match.group(1) or None
    elif text.startswith(("http://", "https://")):
        parsed = urlsplit(text)
        if not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("media URLs must be valid http(s) URLs")
    else:
        try:
            base64.b64decode(text, validate=True)
        except Exception as exc:
            raise ValueError(
                "media must be an http(s) URL or a base64/data-URL attachment"
            ) from exc
    filename = p.get("filename")
    if filename is not None:
        if (
            not isinstance(filename, str)
            or not filename.strip()
            or len(filename) > 255
            or "/" in filename
            or "\\" in filename
        ):
            raise ValueError(
                "filename must be a plain file name of at most 255 characters"
            )
        filename = filename.strip()
    content_type = p.get("content_type")
    if content_type is not None:
        if (
            not isinstance(content_type, str)
            or not content_type.strip()
            or len(content_type) > 128
        ):
            raise ValueError(
                "content_type must be a MIME type of at most 128 characters"
            )
        content_type = content_type.strip()
    return {"media": text, "filename": filename, "content_type": content_type or mime}


def _validate_send_location(p: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a standalone location/venue send. A venue needs both name and address."""
    raw = p.get("location")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {
        "latitude",
        "longitude",
        "accuracy",
        "name",
        "address",
    }:
        raise ValueError(
            "location requires latitude and longitude, with optional accuracy, name, and address"
        )
    for value, name, low, high in (
        (raw.get("latitude"), "latitude", -90, 90),
        (raw.get("longitude"), "longitude", -180, 180),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not low <= value <= high
        ):
            raise ValueError(
                f"location {name} must be a number between {low} and {high}"
            )
    accuracy = raw.get("accuracy")
    if accuracy is not None and (
        isinstance(accuracy, bool)
        or not isinstance(accuracy, (int, float))
        or not 0 <= accuracy <= 1500
    ):
        raise ValueError(
            "location accuracy must be a number of metres between 0 and 1500"
        )
    name, address = raw.get("name"), raw.get("address")
    for key, value in (("name", name), ("address", address)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"location {key} must be a non-empty string")
    if (name is None) != (address is None):
        raise ValueError("location name and address go together: a venue needs both")
    return {
        "latitude": float(raw["latitude"]),
        "longitude": float(raw["longitude"]),
        "accuracy": float(accuracy) if accuracy is not None else None,
        "name": name.strip() if isinstance(name, str) else None,
        "address": address.strip() if isinstance(address, str) else None,
    }


def validate_send_message_payload(
    p: dict[str, Any], *, rich_body, rich_buttons, int_value, quote_excerpt
) -> dict[str, Any]:
    """Validate a generic send_message: recipient plus the full send surface.

    ``rich_body``/``rich_buttons``/``int_value``/``quote_excerpt`` are the
    TelegramClientOperations validators, passed in so this module stays free
    of the operations class. Content is required unless the send carries
    media or a location instead; a location send carries no text or media.
    """
    if set(p) - _SEND_MESSAGE_KEYS:
        raise ValueError(
            "send_message accepts only recipient, content, format, reply_to, thread_id, silent, "
            "buttons, quote, effect, pin, pin_silent, media, filename, content_type, as_voice, "
            "video_note, force_document, as_document, and location; the target is a numeric ID "
            "or @username, never chat_id, peer, or destination"
        )
    media = _validate_send_media(p)
    location = _validate_send_location(p)
    if location is not None and media is not None:
        raise ValueError("location sends cannot be combined with media")
    if location is not None:
        if p.get("content"):
            raise ValueError("location sends cannot be combined with message text")
        if p.get("format") not in (None, "auto"):
            raise ValueError("location sends take no format; omit content and format")
        content, rich_format = "", "auto"
    elif p.get("content"):
        content, rich_format = rich_body(p)
    elif media is not None:
        # Media-only send: the caption is optional, the format still validated.
        content = ""
        rich_format = str(p.get("format", "auto") or "auto").strip().lower()
        if rich_format not in {"auto", "markdown", "html", "blocks"}:
            raise ValueError("format must be auto, markdown, html, or blocks")
    else:
        content, rich_format = rich_body(p)
    reply_to = p.get("reply_to")
    reply_to = int_value(reply_to, "reply_to") if reply_to is not None else None
    thread_id = p.get("thread_id")
    thread_id = int_value(thread_id, "thread_id") if thread_id is not None else None
    quote = p.get("quote")
    if quote is not None:
        if reply_to is None:
            raise ValueError(
                "quote needs reply_to: quote an excerpt of the message being replied to"
            )
        quote = quote_excerpt(p)
    silent = _strict_bool(p, "silent")
    pin = _strict_bool(p, "pin")
    pin_silent = _strict_bool(p, "pin_silent", True)
    as_voice = _strict_bool(p, "as_voice")
    video_note = _strict_bool(p, "video_note")
    force_document = _strict_bool(p, "force_document") or _strict_bool(p, "as_document")
    if as_voice and media is None:
        raise ValueError(
            "as_voice needs media: send an audio attachment as a voice note"
        )
    if video_note and media is None:
        raise ValueError(
            "video_note needs media: send exactly one video attachment as a round video note"
        )
    if as_voice and video_note:
        raise ValueError("as_voice and video_note cannot combine")
    if location is not None and (as_voice or video_note or force_document):
        raise ValueError("location sends take no media flags")
    effect = p.get("effect")
    effect = (
        int_value(effect, "effect", minimum=0, maximum=2**63 - 1)
        if effect is not None
        else None
    )
    return {
        "recipient": validate_recipient(p.get("recipient")),
        "content": content,
        "format": rich_format,
        "reply_to": reply_to,
        "thread_id": thread_id,
        "silent": silent,
        "buttons": rich_buttons(p.get("buttons", [])),
        "quote": quote,
        "effect": effect,
        "pin": pin,
        "pin_silent": pin_silent,
        "media": media["media"] if media else None,
        "filename": media["filename"] if media else None,
        "content_type": media["content_type"] if media else None,
        "as_voice": as_voice,
        "video_note": video_note,
        "force_document": force_document,
        "location": location,
    }
