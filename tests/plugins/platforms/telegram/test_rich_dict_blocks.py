"""Behaviour contracts for plugins/platforms/telegram/rich_dict_blocks.py.

Port of the tg-rich-messages test_rich_ui.py matrix to Telethon layer 233,
plus construction/serialisation checks that the built TL objects round-trip.
No network: media ids come from a fake media map of InputPhoto/InputDocument.
"""
import pytest
from telethon import types

from plugins.platforms.telegram import rich_dict_blocks as blocks_mod


def cell(text="value", **extra):
    return {"text": text, "align": "left", "valign": "top", **extra}


def _photo_map():
    return {"hero": types.InputPhoto(id=123, access_hash=456, file_reference=b"r")}


def _doc_map():
    return {"clip": types.InputDocument(
        id=555, access_hash=666, file_reference=b"d")}


def _photo_block(media="hero", **extra):
    block = {"type": "photo", "media": media}
    block.update(extra)
    return block


class TestValidationMatrix:
    def test_photo_caption_detached_from_input(self):
        block = _photo_block(caption="literal & text")
        result = blocks_mod.blocks_from_dict([block], media=_photo_map())
        block["caption"] = "changed"
        built = result.blocks[0]
        assert isinstance(built, types.PageBlockPhoto)
        assert built.photo_id == 123
        assert built.caption.text.text == "literal & text"

    def test_photo_limits_and_malformed_media(self):
        blocks_mod.blocks_from_dict([_photo_block()] * 50, media=_photo_map())
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict(
                [{"type": "details", "summary": "photos",
                  "blocks": [_photo_block()] * 51}], media=_photo_map())
        for media in ("", None, [], 42):
            with pytest.raises(ValueError):
                blocks_mod.blocks_from_dict([_photo_block(media=media)],
                                            media=_photo_map())
        with pytest.raises(ValueError, match="has no binding"):
            blocks_mod.blocks_from_dict([_photo_block(media="ghost")],
                                        media=_photo_map())
        with pytest.raises(ValueError, match="is a document, not a photo"):
            blocks_mod.blocks_from_dict([_photo_block(media="clip")],
                                        media=_doc_map())
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict(
                [_photo_block(caption="x" * 32769)], media=_photo_map())

    def test_literal_json_detached_and_compact_table(self):
        raw = '{"x":"<script>&\\n\U0001F600"}'
        input_blocks = [
            {"type": "heading", "text": "Result", "size": 2},
            {"type": "table", "is_compact": True,
             "cells": [[cell("Label", is_header=True), cell("Value")]]},
            {"type": "details", "summary": "API",
             "blocks": [{"type": "pre", "text": raw, "language": "json"}]},
        ]
        result = blocks_mod.blocks_from_dict(input_blocks)
        assert result.noautolink is True
        table = result.blocks[1]
        assert isinstance(table, types.PageBlockTable)
        assert table.compact is True
        assert table.rows[0].cells[0].header is True
        assert table.rows[0].cells[0].text.text == "Label"
        assert result.blocks[2].blocks[0].text.text == raw
        input_blocks[1]["cells"][0][0]["text"] = "changed"
        assert result.blocks[1].rows[0].cells[0].text.text == "Label"

    def test_text_exact_boundary_including_nested_summary(self):
        blocks_mod.blocks_from_dict([{"type": "pre", "text": "\U0001F600" * 32768}])
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict(
                [{"type": "details", "summary": "x",
                  "blocks": [{"type": "pre", "text": "x" * 32768}]}])

    def test_table_rows_count_toward_blocks(self):
        blocks_mod.blocks_from_dict([{"type": "table", "cells": [[cell()]] * 499}])
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict([{"type": "table", "cells": [[cell()]] * 500}])

    def test_nesting_and_cycle(self):
        block = {"type": "paragraph", "text": "leaf"}
        for _ in range(15):
            block = {"type": "details", "summary": "s", "blocks": [block]}
        blocks_mod.blocks_from_dict([block])
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict(
                [{"type": "details", "summary": "s", "blocks": [block]}])
        cycle = {"type": "details", "summary": "s"}
        cycle["blocks"] = [cycle]
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict([cycle])

    def test_malformed_block_matrix(self):
        bad = [None, [], [None], [{"type": []}],
               [{"type": "heading", "text": "x", "size": True}],
               [{"type": "heading", "text": "x", "size": 7}],
               [{"type": "paragraph", "text": "\ud800"}],
               [{"type": "pre", "text": "x", "html": "x"}],
               [{"type": "details", "summary": "s", "blocks": [],
                 "is_open": False}],
               [{"type": "table", "cells": [[{"text": "missing alignment"}]]}],
               [{"type": "table", "cells": [[cell(align="diagonal")]]}],
               [{"type": "table", "cells": [[cell()] * 21]}],
               [{"type": "table", "cells": [[cell(colspan=20), cell()]]}],
               [{"type": "table", "cells": [[cell(rowspan=True)]]}],
               [{"type": "table", "cells": [[cell()]], "is_compact": False}]]
        for value in bad:
            with pytest.raises(ValueError):
                blocks_mod.blocks_from_dict(value)

    def test_rich_text_dict_is_accepted_where_rich_ui_wanted_literals(self):
        # The port extends rich_ui: text fields accept RichText dicts.
        result = blocks_mod.blocks_from_dict([
            {"type": "paragraph",
             "text": {"type": "bold", "text": "loud"}}])
        assert isinstance(result.blocks[0].text, types.TextBold)


class TestButtons:
    def test_native_button_actions_and_color(self):
        result = blocks_mod.blocks_from_dict([{
            "type": "button_row",
            "buttons": [
                {"text": "Run", "callback_data": "run", "style": "primary"},
                {"text": "Copy", "copy_text": "literal"},
                {"text": "Open", "url": "tg://resolve?domain=example"},
                {"text": "Find", "switch_inline_query": "q"},
            ]}])
        row = result.blocks[0]
        assert isinstance(row, types.PageBlockButtonRow)
        assert len(row.buttons) == 4
        assert isinstance(row.buttons[0].type, types.InlineButtonTypeCallback)
        assert row.buttons[0].type.data == b"run"
        assert row.buttons[0].style.bg_primary is True
        assert isinstance(row.buttons[1].type, types.InlineButtonTypeCopy)
        assert row.buttons[1].type.copy_text == "literal"
        assert isinstance(row.buttons[2].type, types.InlineButtonTypeUrl)
        assert isinstance(row.buttons[3].type, types.InlineButtonTypeSwitchInline)

    def test_callback_utf8_boundary(self):
        blocks_mod.blocks_from_dict([{"type": "button_row", "buttons": [
            {"text": "x", "callback_data": "\U0001F600" * 16}]}])
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict([{"type": "button_row", "buttons": [
                {"text": "x", "callback_data": "\U0001F600" * 16 + "x"}]}])

    def test_invalid_button_matrix(self):
        cases = [[{"text": "x"}],
                 [{"text": "x", "callback_data": ""}],
                 [{"text": "x", "callback_data": "x", "url": "https://example.com"}],
                 [{"text": "x", "callback_data": "x", "style": "nope"}],
                 [{"text": "x", "copy_text": "x" * 257}],
                 [{"text": "x", "copy_text": ""}],
                 [{"text": "x", "url": "javascript:alert(1)"}],
                 [{"text": "x", "url": "https://example.com\n"}],
                 [{"text": ""}, ]]
        for buttons in cases:
            with pytest.raises(ValueError):
                blocks_mod.blocks_from_dict(
                    [{"type": "button_row", "buttons": buttons}])
        with pytest.raises(ValueError):
            blocks_mod.blocks_from_dict([{"type": "button_row", "buttons": [
                {"text": "x", "callback_data": "x"}] * 9}])
        # Deliberate port difference: rich_ui rejects 'link' as an inline
        # keyboard style, but blocks-mode PageButton supports link=True
        # (the copy button in rich_blocks.py uses it), so it is valid here.
        linked = blocks_mod.blocks_from_dict([{"type": "button_row", "buttons": [
            {"text": "x", "callback_data": "x", "style": "link"}]}])
        assert linked.blocks[0].buttons[0].style.link is True


class TestTelethonMapping:
    def test_built_message_serialises_and_round_trips(self):
        result = blocks_mod.blocks_from_dict([
            {"type": "heading", "text": "Title", "size": 1},
            {"type": "paragraph", "text": "body"},
            _photo_block(caption="cap"),
            {"type": "map", "lat": 48.8566, "long": 2.3522},
        ], media=_photo_map())
        assert len(bytes(result)) > 0
        assert result.photos is not None and len(result.photos) == 1
        assert result.photos[0].id == 123
        assert result.blocks[3].zoom == 15  # map default zoom
        assert result.blocks[3].w == 600 and result.blocks[3].h == 300

    def test_animation_and_voice_note_fold_into_video_and_audio(self):
        result = blocks_mod.blocks_from_dict([
            {"type": "animation", "media": "clip"},
            {"type": "voice_note", "media": "clip"},
        ], media=_doc_map())
        assert isinstance(result.blocks[0], types.PageBlockVideo)
        assert isinstance(result.blocks[1], types.PageBlockDocument)
        assert result.blocks[0].video_id == 555
        assert result.blocks[1].document_id == 555

    def test_date_time_format_maps_to_text_date_flags(self):
        result = blocks_mod.blocks_from_dict([{"type": "paragraph", "text": {
            "type": "date_time", "text": "Jan 1", "unix_time": 1767225600,
            "date_time_format": "d M Y, H:i"}}])
        node = result.blocks[0].text
        assert isinstance(node, types.TextDate)
        assert node.short_date is True
        assert node.short_time is True
        assert node.date.year == 2026
        relative = blocks_mod.blocks_from_dict([{"type": "paragraph", "text": {
            "type": "date_time", "text": "soon", "unix_time": 1767225600,
            "date_time_format": "relative"}}])
        assert relative.blocks[0].text.relative is True
        with pytest.raises(ValueError, match="unknown date_time_format"):
            blocks_mod.blocks_from_dict([{"type": "paragraph", "text": {
                "type": "date_time", "text": "x", "unix_time": 1,
                "date_time_format": "qqq"}}])

    def test_reference_nodes_raise_naming_the_node(self):
        for node_type in ("reference", "reference_link", "anchor_link"):
            with pytest.raises(ValueError, match=node_type):
                blocks_mod.blocks_from_dict([{"type": "paragraph", "text": {
                    "type": node_type, "text": "x", "name": "n"}}])

    def test_thinking_is_draft_only(self):
        with pytest.raises(ValueError, match="draft"):
            blocks_mod.blocks_from_dict([{"type": "thinking", "text": "Thinking…"}])
        draft = blocks_mod.blocks_from_dict(
            [{"type": "thinking", "text": "Thinking…"}], draft=True)
        assert isinstance(draft.blocks[0], types.PageBlockThinking)

    def test_open_app_button_has_no_telethon_equivalent(self):
        with pytest.raises(ValueError, match="open_app"):
            blocks_mod.blocks_from_dict([{"type": "button_row", "buttons": [
                {"text": "x", "open_app": "app"}]}])

    def test_collage_and_slideshow_build_from_media_blocks(self):
        result = blocks_mod.blocks_from_dict([
            {"type": "collage", "blocks": [_photo_block()]},
            {"type": "slideshow", "blocks": [_photo_block()]},
        ], media=_photo_map())
        assert isinstance(result.blocks[0], types.PageBlockCollage)
        assert isinstance(result.blocks[1], types.PageBlockSlideshow)
        assert result.blocks[0].items[0].photo_id == 123
