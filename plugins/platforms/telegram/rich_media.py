"""Media resolution and binding validation for Telegram rich messages (MTProto).

Port of the media layer from serejaris/telegram-skills (Bot API 10.3,
``tg-rich-messages`` / ``tg-markdown-to-rich``) to Telethon layer 233.

Bot API ``InputRichMessageMedia{id, media}`` has no Telethon equivalent, and
neither ``file_id`` strings, HTTP URLs, nor ``attach://`` names can be pasted
into a rich payload. Every source form is resolved first to a server-side
``Photo`` / ``Document`` object, and only then bound:

- ``file_id`` -> ``telethon.utils.resolve_bot_file_id`` (``None`` = invalid)
  -> ``get_input_photo`` / ``get_input_document``.
- HTTP(S) URL -> ``InputMediaPhotoExternal`` / ``InputMediaDocumentExternal``
  -> ``messages.UploadMediaRequest``.
- Local path -> ``client.upload_file`` -> ``InputMediaUploadedPhoto`` /
  ``InputMediaUploadedDocument`` -> ``messages.UploadMediaRequest``.

The resolved objects bind in two places: ``InputRichFilePhoto`` /
``InputRichFileDocument`` in the ``files=`` list of ``InputRichMessageHTML`` /
``InputRichMessageMarkdown`` (string modes), and the bare integer ids in
``PageBlockPhoto`` / ``PageBlockVideo`` / ``PageBlockAudio`` /
``PageBlockDocument`` with the full objects in ``InputRichMessage(photos=...,
documents=...)`` (blocks mode).

Metadata loss (deliberate, recorded): ``duration`` / ``performer`` / ``title``
have no field on ``InputRichFilePhoto`` / ``InputRichFileDocument`` — they only
survive as ``DocumentAttribute*`` on an upload, i.e. when the media is sent
as its own message or resolved through the upload path. ``has_spoiler``
survives on ``InputMediaUploaded*`` and on ``PageBlockPhoto`` / ``PageBlockVideo``.
There is no caption parameter on any ``InputMedia*`` or ``InputRichFile*``:
captions live only on ``PageCaption`` (blocks) or the markup title /
``<figcaption>`` (string modes). ``attach://`` + multipart upload is a Bot
API-only concept and is not ported.
"""
from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

MAX_RICH_MEDIA = 50

MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MEDIA_REF_RE = re.compile(r"tg://(photo|video|audio|document)\?id=([A-Za-z0-9_-]+)")
MEDIA_REF_CANDIDATE_RE = re.compile(
    r"""tg://(photo|video|audio|document)\?id=([^\s)"']+)"""
)
# Bot API media types accepted on a binding. ``document`` is a Hermes
# addition: the spec supports tg://document?id= even though md2rich's own
# regex omits it; Telethon covers it via InputRichFileDocument.
MEDIA_TYPES = {"animation", "audio", "document", "photo", "video", "voice_note"}
REFERENCE_MEDIA_TYPES = {
    "photo": {"photo"},
    "video": {"video", "animation"},
    "audio": {"audio", "voice_note"},
    "document": {"document"},
}
# Which InputRichFile kind a resolved binding becomes. Audio, video,
# animation, voice_note and document are all Documents in MTProto.
_MEDIA_KIND_IS_PHOTO = {"photo": True}


@dataclass(frozen=True)
class ResolvedRichMedia:
    """One resolved media binding, ready to bind into a rich payload."""

    media_id: str
    kind: str  # "photo" or "document"
    input: Any  # InputPhoto | InputDocument
    photo: Any = None  # Photo object, for InputRichMessage(photos=[...])
    document: Any = None  # Document object, for InputRichMessage(documents=[...])

    @property
    def numeric_id(self) -> int:
        """Server-side integer id for PageBlockPhoto/Video/Audio/Document."""
        obj = self.photo if self.photo is not None else self.document
        return int(obj.id)

    def rich_file(self) -> Any:
        """Build the InputRichFilePhoto / InputRichFileDocument for files=[...]."""
        from telethon import types

        if self.kind == "photo":
            return types.InputRichFilePhoto(id=self.media_id, photo=self.input)
        return types.InputRichFileDocument(id=self.media_id, document=self.input)


def extract_media_refs(markup: str) -> list[tuple[str, str]]:
    """Return the (scheme, id) pairs referenced as tg://...?id= in markup."""
    return MEDIA_REF_RE.findall(str(markup or ""))


def validate_media_item(item: dict) -> str:
    """Validate one media binding; return its id. Raises ValueError."""
    if not isinstance(item, dict):
        raise ValueError("Each media binding must be an object.")
    media_id = item.get("id")
    media_object = item.get("media")
    if not isinstance(media_id, str) or not MEDIA_ID_RE.fullmatch(media_id):
        raise ValueError(
            "Media id must be 1-64 characters using only A-Z, a-z, 0-9, _ and -."
        )
    if not isinstance(media_object, dict):
        raise ValueError(f"Media {media_id!r} must contain an InputMedia object.")
    media_type = media_object.get("type")
    source = media_object.get("media")
    if media_type not in MEDIA_TYPES:
        raise ValueError(
            f"Media {media_id!r} has unsupported type {media_type!r}; "
            f"choose one of {', '.join(sorted(MEDIA_TYPES))}."
        )
    if not isinstance(source, str) or not source:
        raise ValueError(f"Media {media_id!r} requires a non-empty media source.")
    return media_id


def validate_rich_media_bindings(markup: str, media: Iterable[dict]) -> None:
    """Validate tg:// references and media bindings, both ways.

    A reference without a binding never renders; a binding without a
    reference is an editing slip Telegram does not report. Raises
    ValueError naming the offending id. Local only, no network.
    """
    media = list(media or [])
    if len(media) > MAX_RICH_MEDIA:
        raise ValueError(
            f"Too many media bindings: {len(media)}, limit is {MAX_RICH_MEDIA}."
        )
    for _, media_id in MEDIA_REF_CANDIDATE_RE.findall(str(markup or "")):
        if not MEDIA_ID_RE.fullmatch(media_id):
            raise ValueError(
                f"Invalid tg:// media id {media_id!r}; use 1-64 characters from "
                "A-Z, a-z, 0-9, _ and -."
            )
    refs = extract_media_refs(markup)
    by_id: dict[str, dict] = {}
    for item in media:
        media_id = validate_media_item(item)
        if media_id in by_id:
            raise ValueError(f"Duplicate media id: {media_id!r}.")
        by_id[media_id] = item["media"]

    for ref_type, media_id in refs:
        if media_id not in by_id:
            raise ValueError(
                f"Media reference tg://{ref_type}?id={media_id} has no binding."
            )
        media_type = by_id[media_id]["type"]
        if media_type not in REFERENCE_MEDIA_TYPES[ref_type]:
            raise ValueError(
                f"Media reference tg://{ref_type}?id={media_id} is incompatible "
                f"with media type {media_type!r}."
            )

    unused = sorted(by_id.keys() - {media_id for _, media_id in refs})
    if unused:
        raise ValueError(
            "Media binding(s) never referenced by the markup: "
            + ", ".join(unused)
            + ". Remove them or add a tg:// reference."
        )


def _upload_media_result(media: Any) -> tuple[Any, Any]:
    """Split an UploadMediaRequest result into (photo, document)."""
    from telethon import types

    if isinstance(media, types.MessageMediaPhoto):
        return media.photo, None
    if isinstance(media, types.MessageMediaDocument):
        return None, media.document
    raise ValueError("UploadMediaRequest did not return photo or document media")


def _attributes_for_upload(media_type: str, binding: Mapping[str, Any],
                           file_name: str) -> list[Any]:
    """Player metadata for an upload. Only expressible on the upload itself."""
    from telethon import types

    attributes: list[Any] = [types.DocumentAttributeFilename(file_name)]
    duration = binding.get("duration")
    if media_type in {"audio", "voice_note"}:
        attributes.append(types.DocumentAttributeAudio(
            duration=int(duration or 0),
            voice=bool(media_type == "voice_note"),
            title=binding.get("title") or None,
            performer=binding.get("performer") or None,
        ))
    elif media_type in {"video", "animation"}:
        attributes.append(types.DocumentAttributeVideo(
            duration=float(duration or 0),
            w=int(binding.get("width") or 0),
            h=int(binding.get("height") or 0),
            supports_streaming=True,
        ))
        if media_type == "animation":
            attributes.append(types.DocumentAttributeAnimated())
    return attributes


async def resolve_rich_media(binding: Mapping[str, Any], *, client: Any,
                             peer: Any) -> ResolvedRichMedia:
    """Resolve one validated binding to server-side media.

    ``binding`` is ``{"id": ..., "media": {"type": ..., "media": source, ...}}``
    with optional ``duration`` / ``performer`` / ``title`` / ``width`` /
    ``height`` / ``has_spoiler`` metadata. ``source`` is a Bot API ``file_id``,
    an HTTP(S) URL, or a local path. Raises ValueError for an unresolvable
    source. The ``client`` must be the connected Telethon client; ``peer`` is
    the resolved InputPeer the upload is addressed to.
    """
    from telethon import functions, types
    from telethon.utils import get_input_document, get_input_photo, resolve_bot_file_id

    media_id = validate_media_item(dict(binding))
    media_object = dict(binding["media"])
    media_type = str(media_object["type"])
    source = str(media_object["media"])
    spoiler = bool(binding.get("has_spoiler") or media_object.get("has_spoiler"))
    kind = "photo" if _MEDIA_KIND_IS_PHOTO.get(media_type) else "document"

    if source.startswith(("http://", "https://")):
        parsed = urlsplit(source)
        if not parsed.hostname:
            raise ValueError(f"Media {media_id!r} has an invalid URL source.")
        if kind == "photo":
            input_media = types.InputMediaPhotoExternal(url=source, spoiler=spoiler or None)
        else:
            input_media = types.InputMediaDocumentExternal(url=source, spoiler=spoiler or None)
        uploaded = await client(functions.messages.UploadMediaRequest(peer=peer, media=input_media))
        photo, document = _upload_media_result(uploaded)
    elif os.path.isfile(source):
        file_name = os.path.basename(source)
        mime_type, _ = mimetypes.guess_type(source)
        mime_type = mime_type or "application/octet-stream"
        uploaded_file = await client.upload_file(source)
        if kind == "photo":
            input_media = types.InputMediaUploadedPhoto(file=uploaded_file, spoiler=spoiler or None)
        else:
            input_media = types.InputMediaUploadedDocument(
                file=uploaded_file, mime_type=mime_type,
                attributes=_attributes_for_upload(media_type, binding, file_name),
                spoiler=spoiler or None,
            )
        uploaded = await client(functions.messages.UploadMediaRequest(peer=peer, media=input_media))
        photo, document = _upload_media_result(uploaded)
    else:
        resolved = resolve_bot_file_id(source)
        if resolved is None:
            raise ValueError(
                f"Media {media_id!r} source is not a local file, an HTTP(S) URL, "
                "or a valid Telegram file_id."
            )
        if isinstance(resolved, types.Photo):
            photo, document = resolved, None
        elif isinstance(resolved, types.Document):
            photo, document = None, resolved
        else:  # pragma: no cover - resolve_bot_file_id returns Photo/Document/None
            raise ValueError(f"Media {media_id!r} file_id did not resolve to media.")

    if kind == "photo":
        if photo is None:
            raise ValueError(f"Media {media_id!r} did not resolve to a photo.")
        return ResolvedRichMedia(media_id=media_id, kind="photo",
                                 input=get_input_photo(photo), photo=photo)
    if document is None:
        raise ValueError(f"Media {media_id!r} did not resolve to a document.")
    return ResolvedRichMedia(media_id=media_id, kind="document",
                             input=get_input_document(document), document=document)


async def bind_rich_media(markup: str, media: Iterable[dict], *, client: Any,
                          peer: Any) -> list[Any]:
    """Validate bindings against markup and resolve them to InputRichFile list.

    Returns the ``files=[...]`` list for ``InputRichMessageHTML`` /
    ``InputRichMessageMarkdown``. Raises ValueError on any binding problem.
    """
    media = list(media or [])
    validate_rich_media_bindings(markup, media)
    resolved = [await resolve_rich_media(item, client=client, peer=peer) for item in media]
    if len(resolved) > MAX_RICH_MEDIA:
        raise ValueError(
            f"Too many media files: {len(resolved)}, limit is {MAX_RICH_MEDIA}."
        )
    return [item.rich_file() for item in resolved]
