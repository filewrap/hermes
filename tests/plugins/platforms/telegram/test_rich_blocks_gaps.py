"""Behaviour contracts for the tg-markdown-to-rich gaps closed in rich_blocks.py.

Covers the inline constructs Telethon's Markdown parser drops, mailto:/tel:
distinctions, nested blockquotes, the footnote fallback, the block-level
HTML constructs, and the _validate_native_blocks hardening (media cap,
char count, depth). No network.
"""
import pytest
from telethon import types

from plugins.platforms.telegram import rich_blocks
from plugins.platforms.telegram.mtproto_rich import input_rich_message


def _plain(node):
    return rich_blocks._rich_text_plain(node)


def _paragraph_text(block):
    return _plain(block.text)


class TestInlinePrePass:
    def test_marked_spoiler_italic_sub_sup(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "A ==marked== B ||hidden|| C *ital* D <sub>2</sub> E <sup>3</sup>")
        assert len(blocks) == 1
        text = blocks[0].text
        assert isinstance(text, types.TextConcat)
        kinds = [type(node).__name__ for node in text.texts]
        assert kinds == ["TextPlain", "TextMarked", "TextPlain", "TextSpoiler",
                         "TextPlain", "TextItalic", "TextPlain",
                         "TextSubscript", "TextPlain", "TextSuperscript"]

    def test_double_star_stays_bold(self):
        blocks = rich_blocks._markdown_segment_to_blocks("**bold** and *ital*")
        kinds = []

        def walk(node):
            if type(node).__name__ == "TextConcat":
                for item in node.texts:
                    walk(item)
            else:
                kinds.append(type(node).__name__)

        walk(blocks[0].text)
        assert kinds == ["TextBold", "TextPlain", "TextItalic"]

    def test_constructs_inside_code_stay_literal(self):
        blocks = rich_blocks._markdown_segment_to_blocks("`==not marked==` and `*not ital*`")
        assert _paragraph_text(blocks[0]) == "==not marked== and *not ital*"


class TestMailtoTel:
    def test_mailto_and_tel_links_become_email_and_phone(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "[mail](mailto:a@example.com) [call](tel:+123)")
        nodes = blocks[0].text.texts
        assert isinstance(nodes[0], types.TextEmail)
        assert nodes[0].email == "a@example.com"
        assert isinstance(nodes[2], types.TextPhone)
        assert nodes[2].phone == "+123"


class TestBlockquote:
    def test_nested_quote_flattens_without_losing_text(self):
        blocks = rich_blocks._markdown_segment_to_blocks("> outer\n> > inner\n> back")
        assert len(blocks) == 1
        assert isinstance(blocks[0], types.PageBlockBlockquote)
        assert "outer" in _paragraph_text(blocks[0])
        assert "inner" in _paragraph_text(blocks[0])
        assert "back" in _paragraph_text(blocks[0])


class TestFootnotes:
    def test_refs_become_superscripts_and_defs_append(self):
        result = input_rich_message(
            "Claim[^a] and more[^b].\n\n[^a]: First source.\n[^b]: Second source.\n",
            rich_format="blocks")
        texts = [_paragraph_text(b) for b in result.blocks
                 if type(b).__name__ == "PageBlockParagraph"]
        assert texts[0] == "Claim¹ and more²."
        assert texts[1] == "1. First source.\n2. Second source."

    def test_undefined_ref_stays_literal_and_code_ref_stays_literal(self):
        result = input_rich_message(
            "No def[^x].\n\n```\ncode [^y]\n```\n", rich_format="blocks")
        assert "[^x]" in _paragraph_text(result.blocks[0])
        assert "[^y]" in _paragraph_text(result.blocks[1])


class TestCustomHtmlBlocks:
    def test_details_aside_map_and_images(self):
        result = input_rich_message(
            "<details>\n<summary>More</summary>\nHidden text.\n</details>\n\n"
            "<aside>Pull this.</aside>\n\n"
            '<tg-map lat="48.8566" long="2.3522" zoom="14" width="800" height="400"/>\n\n'
            "![alt](tg://photo?id=123)\n",
            rich_format="blocks")
        kinds = [type(b).__name__ for b in result.blocks]
        assert kinds == ["PageBlockDetails", "PageBlockPullquote",
                         "PageBlockMap", "PageBlockPhoto"]
        details = result.blocks[0]
        assert _plain(details.title) == "More"
        assert _paragraph_text(details.blocks[0]) == "Hidden text."
        assert _plain(result.blocks[1].text) == "Pull this."
        assert result.blocks[2].zoom == 14
        assert result.blocks[2].w == 800 and result.blocks[2].h == 400
        assert result.blocks[3].photo_id == 123
        assert _plain(result.blocks[3].caption.text) == "alt"

    def test_map_defaults(self):
        result = input_rich_message(
            '<tg-map lat="1.5" long="2.5"/>', rich_format="blocks")
        assert result.blocks[0].zoom == 15
        assert result.blocks[0].w == 600 and result.blocks[0].h == 300

    def test_collage_and_slideshow_from_image_lines(self):
        result = input_rich_message(
            "<tg-collage>\n"
            '<img src="tg://photo?id=11"/>\n'
            '<img src="tg://video?id=22"/>\n'
            "</tg-collage>\n\n"
            "<tg-slideshow>\n"
            "![a](tg://photo?id=33)\n"
            "</tg-slideshow>\n",
            rich_format="blocks")
        assert isinstance(result.blocks[0], types.PageBlockCollage)
        assert isinstance(result.blocks[1], types.PageBlockSlideshow)
        assert result.blocks[0].items[0].photo_id == 11
        assert result.blocks[0].items[1].video_id == 22
        assert result.blocks[1].items[0].photo_id == 33

    def test_string_media_id_stays_plain_markdown(self):
        # The blocks route has no client: only numeric ids build media.
        # The tg:// URL survives as a link node; no media block is built.
        result = input_rich_message(
            "![alt](tg://photo?id=hero)\n", rich_format="blocks")
        assert isinstance(result.blocks[0], types.PageBlockParagraph)
        nodes = result.blocks[0].text.texts
        assert any(isinstance(node, types.TextUrl)
                   and node.url == "tg://photo?id=hero" for node in nodes)


class TestNativeValidation:
    def test_fifty_one_media_blocks_fail(self):
        blocks = [types.PageBlockPhoto(
            photo_id=i, caption=types.PageCaption(
                text=types.TextEmpty(), credit=types.TextEmpty()))
            for i in range(51)]
        with pytest.raises(ValueError, match="50"):
            rich_blocks._validate_native_blocks(blocks)

    def test_text_over_32768_chars_fails(self):
        blocks = [types.PageBlockParagraph(
            text=types.TextPlain(text="x" * 32769))]
        with pytest.raises(ValueError, match="32768"):
            rich_blocks._validate_native_blocks(blocks)

    def test_sixteen_nested_details_fail(self):
        inner = types.PageBlockParagraph(text=types.TextPlain(text="leaf"))
        block = inner
        for _ in range(16):
            block = types.PageBlockDetails(
                blocks=[block], title=types.TextPlain(text="s"))
        with pytest.raises(ValueError, match="nested"):
            rich_blocks._validate_native_blocks([block])

    def test_fifteen_nested_details_pass(self):
        inner = types.PageBlockParagraph(text=types.TextPlain(text="leaf"))
        block = inner
        for _ in range(15):
            block = types.PageBlockDetails(
                blocks=[block], title=types.TextPlain(text="s"))
        rich_blocks._validate_native_blocks([block])
