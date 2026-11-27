"""Blocks -> Rich HTML conversion for badged Markdown finals.

A badged Markdown reply converts its body to Rich HTML so the badges can
ride as literal <tg-button> pills. Every block and inline node the Markdown
segmenter produces must render, and anything without an HTML form (media,
maps, galleries, checklist items, a button that is not a copy button) must
return None so the caller falls back to the blocks route.
"""

from __future__ import annotations

from telethon import types as _tl

from plugins.platforms.telegram.mtproto_rich import blocks_to_rich_html, rich_text_to_html
from plugins.platforms.telegram.rich_blocks import (
    _markdown_segment_to_blocks,
    native_math_blocks,
)


def _html(markdown: str) -> str | None:
    """Render Markdown through the segmenter and the HTML converter."""
    return blocks_to_rich_html(_markdown_segment_to_blocks(markdown))


def test_paragraph_bold_italic_and_link():
    html = _html("Some **bold** and *italic* text with [a link](https://example.com).")
    assert html == ('<p>Some <b>bold</b> and <i>italic</i> text with '
                    '<a href="https://example.com">a link</a>.</p>')


def test_headings_and_divider():
    html = _html("# One\n\n## Two\n\n---\n\nBody.")
    assert html == "<h1>One</h1><h2>Two</h2><hr><p>Body.</p>"


def test_lists_and_nested_lists():
    html = _html("- one\n- two\n  - nested\n\n1. first\n2. second")
    assert html == ("<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>"
                    "<ol><li>first</li><li>second</li></ol>")


def test_fenced_code_keeps_language_and_escapes():
    html = _html("```python\nif a < b:\n    print('x')\n```")
    assert html == "<pre language=\"python\">if a &lt; b:\n    print(&#x27;x&#x27;)</pre>"


def test_blockquote_and_pipe_table():
    html = _html("> a quote\n\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert html == ("<blockquote>a quote</blockquote>"
                    "<table><tr><th>A</th><th>B</th></tr>"
                    "<tr><td>1</td><td>2</td></tr></table>")


def test_details_collapsible():
    html = _html("<details>\n<summary>More</summary>\n\nHidden **text**.\n\n</details>")
    assert html == ("<details><summary>More</summary>"
                    "<p>Hidden <b>text</b>.</p></details>")


def test_display_math_converts_to_math_block():
    blocks = native_math_blocks("The sum is $$x^2 + y^2$$").blocks
    html = blocks_to_rich_html(blocks)
    assert html is not None
    assert "<tg-math-block>x^2 + y^2</tg-math-block>" in html
    assert "$$" not in html


def test_inline_math_converts_to_math_span():
    blocks = native_math_blocks("The value $x^2$ grows.").blocks
    html = blocks_to_rich_html(blocks)
    assert html is not None
    assert "<tg-math>x^2</tg-math>" in html
    assert "$" not in html.replace("tg-math", "")


def test_spoiler_marked_sub_superscript():
    html = _html("||secret|| and ==marked== and H<sub>2</sub>O and x<sup>2</sup>.")
    assert html == ("<p><tg-spoiler>secret</tg-spoiler> and <mark>marked</mark> "
                    "and H<sub>2</sub>O and x<sup>2</sup>.</p>")


def test_copy_button_survives_as_html_button():
    node = _tl.TextButton(
        text=_tl.TextFixed(text=_tl.TextPlain(text="/stop")),
        type=_tl.InlineButtonTypeCopy(copy_text="/stop"),
        style=_tl.RichButtonStyle(link=True))
    assert rich_text_to_html(node) == '<tg-button type="copy_text" text="/stop">/stop</tg-button>'


def test_mention_name_links_to_the_user():
    node = _tl.TextMentionName(text=_tl.TextPlain(text="Kailash"), user_id=6802929470)
    assert rich_text_to_html(node) == '<a href="tg://user?id=6802929470">Kailash</a>'


def test_unknown_rich_text_node_returns_none():
    node = _tl.TextAnchor(text=_tl.TextPlain(text="here"), name="anchor")
    assert rich_text_to_html(node) is None


def test_non_copy_button_returns_none():
    node = _tl.TextButton(
        text=_tl.TextPlain(text="tap"),
        type=_tl.InlineButtonTypeCallback(data=b"fb:m:x|y"),
        style=_tl.RichButtonStyle(bg_primary=True))
    assert rich_text_to_html(node) is None


def test_media_block_returns_none():
    assert _html("Look:\n\n![pic](tg://photo?id=123)") is None


def test_map_block_returns_none():
    assert _html('<tg-map lat="28.6" long="77.2">') is None


def test_collage_block_returns_none():
    html = _html("<tg-collage>\n![a](tg://photo?id=1)\n![b](tg://photo?id=2)\n</tg-collage>")
    assert html is None


def test_checklist_item_returns_none():
    assert _html("- [ ] todo one\n- [x] todo two") is None


def test_empty_blocks_render_empty():
    assert blocks_to_rich_html([]) == ""
    assert rich_text_to_html(_tl.TextEmpty()) == ""
    assert rich_text_to_html(None) == ""
