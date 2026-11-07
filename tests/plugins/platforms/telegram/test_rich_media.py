"""Behaviour contracts for plugins/platforms/telegram/rich_media.py.

Port of the tg-rich-messages / tg-markdown-to-rich media-binding tests to
Telethon layer 233. No network: the client is faked; file_id strings are
built with the same packing Telethon itself uses.
"""
import struct

import pytest
from telethon import types
from telethon.utils import _encode_telegram_base64, _rle_encode

from plugins.platforms.telegram import rich_media


def _pack_photo_file_id(photo_id: int, access_hash: int) -> str:
    """Build a Bot API photo file_id the way pack_bot_file_id does.

    (pack_bot_file_id itself is broken for photos in layer 233: PhotoSize
    no longer carries a location, so it raises AttributeError.)
    """
    raw = struct.pack("<iiqqqqib", 2, 1, photo_id, access_hash, 11, 0, 22, 2)
    return _encode_telegram_base64(_rle_encode(raw))


def _photo(photo_id: int = 123, access_hash: int = 456) -> types.Photo:
    return types.Photo(
        id=photo_id, access_hash=access_hash, file_reference=b"ref",
        date=0, sizes=[types.PhotoSize(type="s", w=1, h=1, size=1)], dc_id=1,
    )


def _document(doc_id: int = 555, access_hash: int = 666) -> types.Document:
    return types.Document(
        id=doc_id, access_hash=access_hash, file_reference=b"dref",
        date=0, mime_type="audio/mpeg", size=100, dc_id=1,
        attributes=[types.DocumentAttributeFilename("x.mp3")],
    )


class _FakeClient:
    """Minimal async stand-in for TelegramClient (upload + UploadMedia)."""

    def __init__(self, uploaded=None):
        self.uploaded = uploaded
        self.uploaded_files = []

    async def upload_file(self, path):
        self.uploaded_files.append(path)
        return types.InputFile(id=1, parts=1, name="x", md5_checksum="")

    async def __call__(self, request):
        return self.uploaded


def _binding(media_id, media_type, source, **extra):
    item = {"id": media_id, "media": {"type": media_type, "media": source}}
    item.update(extra)
    return item


class TestValidateMediaItem:
    def test_valid_photo_binding_returns_id(self):
        assert rich_media.validate_media_item(
            _binding("hero", "photo", "AgADAQADFc1bBwAEsWjeOgAECwAPFgADAg")) == "hero"

    def test_rejects_bad_id_characters(self):
        with pytest.raises(ValueError, match="Media id must be 1-64"):
            rich_media.validate_media_item(_binding("bad id!", "photo", "x"))

    def test_rejects_unsupported_type(self):
        with pytest.raises(ValueError, match="unsupported type"):
            rich_media.validate_media_item(_binding("a", "sticker", "x"))

    def test_rejects_empty_source(self):
        with pytest.raises(ValueError, match="non-empty media source"):
            rich_media.validate_media_item(_binding("a", "photo", ""))


class TestValidateRichMediaBindings:
    def test_valid_set_passes(self):
        rich_media.validate_rich_media_bindings(
            '<p><a href="tg://photo?id=hero">see</a></p>',
            [_binding("hero", "photo", "AgADAQADFc1bBwAEsWjeOgAECwAPFgADAg")])

    def test_ref_without_binding_fails(self):
        with pytest.raises(ValueError, match="has no binding"):
            rich_media.validate_rich_media_bindings(
                '<a href="tg://photo?id=ghost">x</a>', [])

    def test_binding_without_ref_fails(self):
        with pytest.raises(ValueError, match="never referenced"):
            rich_media.validate_rich_media_bindings(
                "<p>no refs</p>",
                [_binding("orphan", "photo", "AgADAQADFc1bBwAEsWjeOgAECwAPFgADAg")])

    def test_duplicate_id_fails(self):
        with pytest.raises(ValueError, match="Duplicate media id"):
            rich_media.validate_rich_media_bindings(
                '<a href="tg://photo?id=dup">x</a>',
                [_binding("dup", "photo", "x"), _binding("dup", "photo", "y")])

    def test_incompatible_scheme_and_type_fails(self):
        with pytest.raises(ValueError, match="incompatible"):
            rich_media.validate_rich_media_bindings(
                '<a href="tg://photo?id=v">x</a>',
                [_binding("v", "video", "https://example.com/v.mp4")])

    def test_video_accepts_animation(self):
        rich_media.validate_rich_media_bindings(
            '<a href="tg://video?id=a">x</a>',
            [_binding("a", "animation", "https://example.com/a.mp4")])

    def test_document_reference_accepted(self):
        rich_media.validate_rich_media_bindings(
            '<a href="tg://document?id=d">x</a>',
            [_binding("d", "document", "https://example.com/d.pdf")])

    def test_more_than_fifty_bindings_fails(self):
        with pytest.raises(ValueError, match="Too many media bindings"):
            rich_media.validate_rich_media_bindings(
                "", [_binding(f"m{i}", "photo", "x") for i in range(51)])


class TestResolveRichMedia:
    @pytest.mark.asyncio
    async def test_file_id_photo_resolves_to_input_photo(self):
        file_id = _pack_photo_file_id(123, 456)
        resolved = await rich_media.resolve_rich_media(
            _binding("hero", "photo", file_id),
            client=_FakeClient(), peer=object())
        assert resolved.kind == "photo"
        assert resolved.numeric_id == 123
        assert isinstance(resolved.input, types.InputPhoto)
        assert resolved.input.id == 123
        assert isinstance(resolved.rich_file(), types.InputRichFilePhoto)

    @pytest.mark.asyncio
    async def test_invalid_file_id_fails(self):
        with pytest.raises(ValueError, match="not a local file"):
            await rich_media.resolve_rich_media(
                _binding("bad", "photo", "not-a-real-file-id"),
                client=_FakeClient(), peer=object())

    @pytest.mark.asyncio
    async def test_url_photo_goes_through_upload_media(self):
        client = _FakeClient(uploaded=types.MessageMediaPhoto(photo=_photo()))
        resolved = await rich_media.resolve_rich_media(
            _binding("u", "photo", "https://example.com/p.jpg"),
            client=client, peer=object())
        assert resolved.kind == "photo"
        assert resolved.numeric_id == 123

    @pytest.mark.asyncio
    async def test_local_audio_upload_carries_player_metadata(self, tmp_path):
        song = tmp_path / "song.mp3"
        song.write_bytes(b"fake-audio")
        client = _FakeClient(uploaded=types.MessageMediaDocument(document=_document()))
        resolved = await rich_media.resolve_rich_media(
            _binding("s", "audio", str(song),
                     duration=180, performer="Singer", title="Song"),
            client=client, peer=object())
        assert resolved.kind == "document"
        assert resolved.numeric_id == 555
        assert client.uploaded_files == [str(song)]

    @pytest.mark.asyncio
    async def test_missing_local_file_fails(self):
        with pytest.raises(ValueError, match="not a local file"):
            await rich_media.resolve_rich_media(
                _binding("m", "photo", "/tmp/does-not-exist-12345.jpg"),
                client=_FakeClient(), peer=object())

    @pytest.mark.asyncio
    async def test_bind_validates_then_resolves_to_files(self):
        client = _FakeClient(uploaded=types.MessageMediaPhoto(photo=_photo()))
        files = await rich_media.bind_rich_media(
            '<a href="tg://photo?id=u">x</a>',
            [_binding("u", "photo", "https://example.com/p.jpg")],
            client=client, peer=object())
        assert len(files) == 1
        assert isinstance(files[0], types.InputRichFilePhoto)
        assert files[0].id == "u"

    @pytest.mark.asyncio
    async def test_bind_rejects_unbound_reference(self):
        with pytest.raises(ValueError, match="has no binding"):
            await rich_media.bind_rich_media(
                '<a href="tg://photo?id=ghost">x</a>', [],
                client=_FakeClient(), peer=object())
