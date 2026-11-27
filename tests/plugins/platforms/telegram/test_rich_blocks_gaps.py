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


class TestFencedCodeHasNoCopyButton:
    """Fenced code renders as a bare Preformatted block: Telegram's beta
    client already puts its own copy icon on code blocks, so the Copy
    button row Hermes used to append under short fences is redundant."""

    def test_short_fence_renders_preformatted_with_no_button_row(self):
        result = input_rich_message("```\nprint('hi')\n```\n", rich_format="blocks")
        kinds = [type(block).__name__ for block in result.blocks]
        assert kinds == ["PageBlockPreformatted"]
        assert _plain(result.blocks[0].text) == "print('hi')"

    def test_long_fence_renders_preformatted_with_no_button_row(self):
        code = "x = " + "1" * 150
        result = input_rich_message(f"```\n{code}\n```\n", rich_format="blocks")
        kinds = [type(block).__name__ for block in result.blocks]
        assert kinds == ["PageBlockPreformatted"]
        assert _plain(result.blocks[0].text) == code


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


def _math_sources(node):
    """Sources of every TextMath node under *node*, in order."""
    sources = []

    def walk(item):
        name = type(item).__name__
        if name == "TextMath":
            sources.append(item.source)
        elif name == "TextConcat":
            for child in item.texts:
                walk(child)
        elif hasattr(item, "text"):
            walk(item.text)

    walk(node)
    return sources


class TestCurrencyDollarsAreNotMath:
    """A `$` before an amount is currency, not the opener of an inline math span."""

    def test_two_currency_amounts_stay_plain(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "price is $20 today and $30 tomorrow end")
        assert _paragraph_text(blocks[0]) == "price is $20 today and $30 tomorrow end"
        assert _math_sources(blocks[0].text) == []

    def test_currency_then_real_math_keeps_the_math(self):
        blocks = rich_blocks._markdown_segment_to_blocks("$20 and $x$")
        assert _paragraph_text(blocks[0]) == "$20 and "
        assert _math_sources(blocks[0].text) == ["x"]

    def test_real_math_then_currency_keeps_the_math(self):
        blocks = rich_blocks._markdown_segment_to_blocks("$x + y$ and $20")
        assert _paragraph_text(blocks[0]) == " and $20"
        assert _math_sources(blocks[0].text) == ["x + y"]

    def test_tight_dollar_amount_stays_math(self):
        blocks = rich_blocks._markdown_segment_to_blocks("the answer is $2+2$")
        assert _math_sources(blocks[0].text) == ["2+2"]

    def test_pure_currency_text_stays_on_the_markdown_route(self):
        assert rich_blocks.native_math_blocks(
            "price is $20 today and $30 tomorrow") is None

    def test_real_math_takes_the_blocks_route(self):
        assert rich_blocks.native_math_blocks("$x + y$") is not None


class TestEntityBoundarySpacesPreserved:
    """Spaces next to an inline entity survive the Telethon parse.

    Telethon's Markdown parser strips the leading/trailing whitespace of
    whatever string it is given, so a plain segment split out of a paragraph
    lost the spaces that glued it to its neighbours (``for *you* to`` came
    out as ``foryouto``).
    """

    def test_spaces_around_italic_survive(self):
        text = "Instagram shows just enough for *you* to recognize your own contact info"
        blocks = rich_blocks._markdown_segment_to_blocks(text)
        assert _paragraph_text(blocks[0]) == \
            "Instagram shows just enough for you to recognize your own contact info"

    def test_space_between_two_italics_survives(self):
        blocks = rich_blocks._markdown_segment_to_blocks("buzzes *is* *the* one")
        assert _paragraph_text(blocks[0]) == "buzzes is the one"

    def test_space_after_bold_before_italic_survives(self):
        blocks = rich_blocks._markdown_segment_to_blocks("**bold** and *ital*")
        assert _paragraph_text(blocks[0]) == "bold and ital"


class TestUnclosedAndTildeFences:
    """A fence that never closes is not a code block; ``~~~`` fences work.

    An unclosed fence used to swallow every following line into the pre
    block, so the text after it never showed standalone. A closer must use
    the opener's character and be at least as long (CommonMark).
    """

    def test_unclosed_fence_stays_plain_and_following_text_stands_alone(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "```python\ncode line one\ncode line two\n\n"
            "Third block that should be standalone\nMore trailing text")
        assert [type(block).__name__ for block in blocks] == [
            "PageBlockParagraph", "PageBlockParagraph"]
        assert _paragraph_text(blocks[0]) == "```python code line one code line two"
        assert _paragraph_text(blocks[1]) == \
            "Third block that should be standalone More trailing text"

    def test_tilde_fence_closes_and_renders_code(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "~~~\ncode line\n~~~\n\nAfter")
        assert [type(block).__name__ for block in blocks] == [
            "PageBlockPreformatted", "PageBlockParagraph"]
        assert _plain(blocks[0].text) == "code line"
        assert _paragraph_text(blocks[1]) == "After"

    def test_longer_opener_keeps_shorter_inner_fence(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "````\ncode\n```\nmore\n````\n\nAfter")
        assert [type(block).__name__ for block in blocks] == [
            "PageBlockPreformatted", "PageBlockParagraph"]
        assert _plain(blocks[0].text) == "code\n```\nmore"
        assert _paragraph_text(blocks[1]) == "After"

    def test_closed_fence_still_renders_code(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "```python\ncode line\n```\n\nAfter")
        assert [type(block).__name__ for block in blocks] == [
            "PageBlockPreformatted", "PageBlockParagraph"]
        assert _plain(blocks[0].text) == "code line"
        assert blocks[0].language == "python"

    def test_copyable_code_detection_sees_tilde_fences(self):
        assert rich_blocks.markdown_has_copyable_code("~~~\ncode\n~~~")
        assert rich_blocks.markdown_has_copyable_code("```\ncode\n```")

    def test_four_backtick_fence_is_not_copyable_code(self):
        # recent_logs wraps excerpts in ```` fences so ordinary
        # triple-backticks stay inside code; those messages keep the
        # Markdown route they have always taken (the /logs card).
        assert not rich_blocks.markdown_has_copyable_code(
            "````text\nsome log line\n````")


class TestDenseEntityRunsStayDistinct:
    """A dense run of entities keeps its spaces and its boundaries.

    Normal, italic, bold, bold, italic and code back to back used to bleed
    into each other once the spaces between them were eaten; with the
    currency guard and the edge-space fix each entity stands on its own.
    """

    def test_dense_run_keeps_every_space(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "normal *italic* **bold** **bold** *italic* `code` end")
        assert _paragraph_text(blocks[0]) == \
            "normal italic bold bold italic code end"

    def test_lone_stars_and_currency_amounts_stay_literal(self):
        blocks = rich_blocks._markdown_segment_to_blocks(
            "a * b and $20 vs $30")
        assert _paragraph_text(blocks[0]) == "a * b and $20 vs $30"
        assert _math_sources(blocks[0].text) == []
