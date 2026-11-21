"""Generic send_message execution for telegram_client, MTProto-first.

This is the send surface the external agent's message tool exposes on Telegram, on
MTProto instead of the Bot API: reply to a message in the recipient chat,
send into a forum topic, silent sends, one-way buttons, verbatim quotes,
message effects, pin-after-send, one media attachment (URL or base64) with
the body as its caption, voice/video-note sends, and standalone
location/venue sends. Text keeps the native RichMessage path; media and
location go through SendMediaRequest with the same reply header, so a
topic send lands in the topic instead of outside it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import shutil
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

_MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 120


async def execute_send_message(
    ops: Any, source: Any, p: dict[str, Any]
) -> dict[str, Any]:
    """Send one generic message to the validated recipient, then pin it when asked.

    The recipient peer is resolved at execution time so an unresolvable
    target is refused before any send is attempted, mirroring the old
    text-only branch.
    """
    from dataclasses import replace

    scope_source = replace(
        source, chat_id=p["recipient"], thread_id=None, message_id=None
    )
    try:
        target_peer = await ops._peer(scope_source)
    except Exception as exc:
        raise ValueError(
            f"Telegram could not resolve recipient {p['recipient']}; "
            "use a numeric chat/user ID or a valid @username the bot can reach"
        ) from exc
    header = await _reply_header_for_send(ops, target_peer, p)
    profile = ops._profile(source)
    if p["location"] is not None:
        data = await _send_location(
            ops, source, scope_source, target_peer, p, profile, header
        )
    elif p["media"] is not None:
        data = await _send_media(
            ops, source, scope_source, target_peer, p, profile, header
        )
    else:
        data = await ops._send_rich_message(
            source,
            {"content": p["content"], "format": p["format"], "buttons": p["buttons"]},
            profile,
            reply_header=header,
            scope_source=scope_source,
            silent=p["silent"],
            effect=p["effect"],
        )
    data.update(
        {
            "recipient": p["recipient"],
            "thread_id": str(p["thread_id"]) if p["thread_id"] else None,
            "reply_to_message_id": p["reply_to"],
            "silent": p["silent"],
            "effect": p["effect"],
        }
    )
    if p["pin"]:
        pinned, pin_error = await _pin_sent(
            ops, target_peer, data.get("message_id"), p["pin_silent"]
        )
        data["pinned"] = pinned
        if pin_error:
            # The send already happened; a failed pin must not read as a
            # failed send and invite a duplicate resend.
            data["pin_error"] = pin_error
    return data


async def _reply_header_for_send(ops: Any, target_peer: Any, p: dict[str, Any]) -> Any:
    """Reply header for the recipient chat: reply target, forum topic, quote excerpt.

    The quote offset is Telegram's UTF-16 offset inside the replied-to
    message, so the excerpt is read back from the server and matched
    verbatim, exactly like quote_reply.
    """
    from .topic_delivery import topic_reply_header

    quote_text = None
    quote_offset = None
    if p["quote"] is not None:
        target = await ops._client().get_messages(target_peer, ids=[p["reply_to"]])
        message = target[0] if isinstance(target, (list, tuple)) else target
        if message is None or int(getattr(message, "id", 0) or 0) != int(p["reply_to"]):
            raise ValueError(
                "Telegram could not read the replied-to message; "
                "the bot must be able to see it in the recipient chat."
            )
        target_text = str(getattr(message, "message", "") or "")
        offset = target_text.find(p["quote"])
        if offset < 0:
            raise ValueError(
                "quote must match the replied-to message's text exactly; "
                "read the message first and copy the excerpt verbatim."
            )
        quote_text = p["quote"]
        quote_offset = len(target_text[:offset].encode("utf-16-le")) // 2
    if p["reply_to"] is None and p["thread_id"] is None:
        return None
    return topic_reply_header(
        p["reply_to"], p["thread_id"], quote_text=quote_text, quote_offset=quote_offset
    )


async def _inline_markup(ops: Any, scope: Any, buttons: list[dict[str, Any]]) -> Any:
    """Classic inline keyboard for media/location sends, two buttons per row.

    Same one-way shapes as the rich path's PageButtons: URL, copy text, and
    user profile. The rich path renders these as in-body pills or PageButton
    rows; a media/location request carries them as reply_markup instead.
    """
    if not buttons:
        return None
    from telethon import types, utils

    native = []
    for button in buttons:
        kind, label, value = button["type"], button["label"], button["value"]
        if kind == "url":
            button_type = types.InlineButtonTypeUrl(url=value)
        elif kind == "copy":
            button_type = types.InlineButtonTypeCopy(copy_text=value)
        else:
            target_id = int(value) if value is not None else int(scope.user_id)
            entity = await ops._client().get_input_entity(types.PeerUser(target_id))
            input_user = utils.get_input_user(entity)
            if (
                not isinstance(input_user, types.InputUser)
                or int(input_user.user_id) != target_id
            ):
                raise ValueError("Telegram could not resolve that user's profile")
            button_type = types.InlineButtonTypeUserProfile(user_id=input_user)
        native.append(types.KeyboardInlineButton(text=label, type=button_type))
    return types.ReplyInlineMarkup(
        rows=[
            types.KeyboardButtonRow(buttons=native[index : index + 2])
            for index in range(0, len(native), 2)
        ]
    )


def _media_filename(p: dict[str, Any]) -> str:
    if p["filename"]:
        return p["filename"]
    if p["media"].startswith(("http://", "https://")):
        name = unquote(urlsplit(p["media"]).path.rsplit("/", 1)[-1] or "")
        if name:
            return name
    extension = (
        mimetypes.guess_extension(p["content_type"]) if p["content_type"] else None
    )
    return "attachment" + (extension or "")


def _download_url(url: str) -> bytes:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
    with urllib.request.urlopen(
        request, timeout=_MEDIA_DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        return response.read()


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


async def _materialize_media(p: dict[str, Any]) -> tuple[str, str, str, str]:
    """``(tmpdir, path, filename, mime)``: a local file for the attachment.

    The caller deletes the temp dir. The file keeps its attachment name so
    Telethon's mime/attribute detection sees the real filename.
    """
    filename = _media_filename(p)
    mime = (
        p["content_type"]
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    tmpdir = tempfile.mkdtemp(prefix="hermes-send-")
    path = os.path.join(tmpdir, filename)
    if p["media"].startswith("data:"):
        data = base64.b64decode(p["media"].split(",", 1)[1])
    elif p["media"].startswith(("http://", "https://")):
        data = await asyncio.to_thread(_download_url, p["media"])
    else:
        data = base64.b64decode(p["media"])
    await asyncio.to_thread(_write_file, path, data)
    return tmpdir, path, filename, mime


def _caption_text(p: dict[str, Any]) -> tuple[str, list[Any]]:
    """Caption text and entities for a media send.

    Blocks bodies have no entity form, so the caption carries the raw body,
    the same companion text the rich path sends alongside an InputRichMessage.
    """
    if not p["content"]:
        return "", []
    if p["format"] == "blocks":
        return p["content"], []
    from plugins.platforms.telegram.mtproto_rich import text_and_entities

    return text_and_entities(p["content"], p["format"])


def _receipt_id(
    ops: Any, response: Any, request: Any, target_peer: Any, thread_id: Any
) -> Any:
    """Sent message id from the adapter's receipt parser when it has one.

    Mirrors _send_rich_message: the adapter owns receipt parsing, and the
    topic_delivery helper is the fallback, never a second owner.
    """
    receipt = getattr(ops.adapter, "_sent_message_id", None)
    if callable(receipt):
        return receipt(
            response, random_id=request.random_id, peer=target_peer, topic_id=thread_id
        )
    from .topic_delivery import sent_message_id

    return sent_message_id(
        response, random_id=request.random_id, peer=target_peer, topic_id=thread_id
    )


async def _send_media(
    ops: Any,
    source: Any,
    scope_source: Any,
    target_peer: Any,
    p: dict[str, Any],
    profile: str,
    header: Any,
) -> dict[str, Any]:
    """Send one attachment with the body as its caption, via SendMediaRequest.

    Uploaded with Telethon's own media detection (photo vs document, voice
    and video-note attributes), then sent as a raw request so the reply
    header carries the forum topic and quote exactly like a text send.
    """
    from telethon import functions, utils

    tmpdir, path, _filename, mime = await _materialize_media(p)
    try:
        client = ops._client()
        handle = await client.upload_file(path)
        is_photo = (
            mime.startswith("image/")
            and not p["force_document"]
            and not p["as_voice"]
            and not p["video_note"]
        )
        media = utils.get_input_media(
            handle,
            is_photo=is_photo,
            force_document=p["force_document"],
            voice_note=p["as_voice"],
            video_note=p["video_note"],
        )
        caption, entities = _caption_text(p)
        markup = await _inline_markup(ops, scope_source, p["buttons"])
        allowed, reason = ops._authorize(source)
        if not allowed or ops._profile(source) != profile:
            raise ValueError(
                reason or "The Telegram route changed before the message could be sent"
            )
        request = functions.messages.SendMediaRequest(
            peer=target_peer,
            media=media,
            message=caption,
            entities=entities or None,
            silent=p["silent"],
            reply_to=header,
            reply_markup=markup,
            effect=p["effect"],
        )
        response = await ops.adapter._mtproto_rich_request(request)
        sent_id = _receipt_id(ops, response, request, target_peer, p["thread_id"])
        if sent_id:
            ops.observe(
                scope_source,
                type(
                    "ObservedSentMessage",
                    (),
                    {
                        "id": int(sent_id),
                        "message": caption,
                        "sender_id": source.user_id,
                        "media": None,
                    },
                )(),
            )
        return {
            "message_id": int(sent_id) if sent_id else None,
            "chat_id": str(scope_source.chat_id),
            "thread_id": str(p["thread_id"]) if p["thread_id"] else None,
            "button_count": len(p["buttons"]),
            "media": True,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _send_location(
    ops: Any,
    source: Any,
    scope_source: Any,
    target_peer: Any,
    p: dict[str, Any],
    profile: str,
    header: Any,
) -> dict[str, Any]:
    """Send a standalone pin or venue, via SendMediaRequest with the same header."""
    from telethon import functions, types

    location = p["location"]
    geo = types.InputGeoPoint(
        lat=location["latitude"],
        long=location["longitude"],
        accuracy_radius=location["accuracy"],
    )
    if location["name"] is not None:
        media = types.InputMediaVenue(
            geo_point=geo,
            title=location["name"],
            address=location["address"],
            provider="",
            venue_id="",
            venue_type="",
        )
    else:
        media = types.InputMediaGeoPoint(geo_point=geo)
    markup = await _inline_markup(ops, scope_source, p["buttons"])
    allowed, reason = ops._authorize(source)
    if not allowed or ops._profile(source) != profile:
        raise ValueError(
            reason or "The Telegram route changed before the message could be sent"
        )
    request = functions.messages.SendMediaRequest(
        peer=target_peer,
        media=media,
        message="",
        silent=p["silent"],
        reply_to=header,
        reply_markup=markup,
        effect=p["effect"],
    )
    response = await ops.adapter._mtproto_rich_request(request)
    sent_id = _receipt_id(ops, response, request, target_peer, p["thread_id"])
    if sent_id:
        ops.observe(
            scope_source,
            type(
                "ObservedSentMessage",
                (),
                {
                    "id": int(sent_id),
                    "message": "",
                    "sender_id": source.user_id,
                    "media": None,
                },
            )(),
        )
    return {
        "message_id": int(sent_id) if sent_id else None,
        "chat_id": str(scope_source.chat_id),
        "thread_id": str(p["thread_id"]) if p["thread_id"] else None,
        "button_count": len(p["buttons"]),
        "location": True,
    }


async def _pin_sent(
    ops: Any, target_peer: Any, message_id: Any, silent: bool
) -> tuple[bool, str | None]:
    """Pin the just-sent message; ``(pinned, pin_error)`` — never raises."""
    if not message_id:
        return (
            False,
            "Telegram did not confirm a message id, so the message could not be pinned",
        )
    from telethon import functions

    try:
        await ops.adapter._mtproto_rich_request(
            functions.messages.UpdatePinnedMessageRequest(
                peer=target_peer, id=int(message_id), silent=bool(silent)
            )
        )
    except Exception as exc:
        # Deliberate catch-all boundary: the send already happened, so any
        # pin failure is reported on the result instead of failing the call
        # and inviting a duplicate resend.
        logger.warning(
            "Pin after send_message failed for message %s", message_id, exc_info=True
        )
        return False, ops._safe_error(exc)
    return True, None
