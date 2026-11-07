from telethon.tl.types import MessageEntityBold, MessageEntityTextUrl

from plugins.platforms.telegram.mtproto_rich import (
    detect_rich_format, input_rich_html, input_rich_markdown, text_and_entities,
)


def test_detects_html_markdown_and_plain_text():
    assert detect_rich_format("<p>hello</p>") == "html"
    assert detect_rich_format("## heading\n**bold**") == "markdown"
    assert detect_rich_format("ordinary text & symbols") == "plain"


def test_html_table_becomes_plain_layout_with_real_text_and_entities():
    html = "<h3>Status</h3><table><tr><th>Detail</th><th>Value</th></tr>" \
           "<tr><td>Title</td><td><strong>Plan mode | with no task</strong></td></tr></table>"
    text, entities = text_and_entities(html)
    assert "Title" in text
    assert "Plan mode | with no task" in text
    assert "<table>" not in text
    assert any(isinstance(entity, MessageEntityBold) for entity in entities)


def test_html_injection_is_escaped_and_links_become_entities():
    text, entities = text_and_entities(
        '<p>&lt;script&gt;alert(1)&lt;/script&gt; '
        '<a href="https://example.org/?a=1&amp;b=2">safe</a></p>'
    )
    assert text == "<script>alert(1)</script> safe"
    links = [entity for entity in entities if isinstance(entity, MessageEntityTextUrl)]
    assert len(links) == 1
    assert links[0].url == "https://example.org/?a=1&b=2"


def test_markdown_uses_canonical_parser_and_plain_mode_keeps_literal_markup():
    text, entities = text_and_entities("**hello**")
    assert text == "hello"
    assert any(isinstance(entity, MessageEntityBold) for entity in entities)
    assert text_and_entities("**literal**", rich_format="plain")[0] == "**literal**"


def test_page_indexation_is_canonical_italic_and_final_in_html_and_markdown():
    html = input_rich_html(
        "<p>Before page indexation</p><p><i>Page 2 of 3 · 21 providers</i></p>"
    ).html
    assert html.endswith("<p><i>Page 2 of 3 • total 21</i></p>")
    assert html.index("Before page indexation") < html.index("Page 2 of 3")

    markdown = input_rich_markdown(
        "# Body\n\n__Page 2 of 3 • Total 21__\n\nA notice after the old footer."
    ).markdown
    assert markdown.endswith("__Page 2 of 3 • total 21__")
    assert markdown.index("A notice after the old footer.") < markdown.index("Page 2 of 3")


def test_single_page_indexation_without_count_gets_italic_total_one():
    html = input_rich_html("<p>Body</p><p>Page 1 of 1</p>").html
    assert html.endswith("<p><i>Page 1 of 1 • total 1</i></p>")


def _plain_texts(node, out):
    if isinstance(node, dict):
        if node.get("_") == "TextPlain" and isinstance(node.get("text"), str):
            out.append(node["text"])
        for value in node.values():
            _plain_texts(value, out)
    elif isinstance(node, list):
        for item in node:
            _plain_texts(item, out)


def test_single_cell_table_row_becomes_a_spanning_section_head():
    from telethon import types

    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message(
        "| Item | Details |\n"
        "|---|---|\n"
        "| **Start** |\n"
        "| Begin | Send `/deepresearch <question>`. |\n"
        "| **Controls** |\n"
        "| `/deepresearch stop` | Stop the run. |\n",
        rich_format="blocks",
    )
    assert isinstance(rich, types.InputRichMessage)
    tables = [block for block in rich.blocks if isinstance(block, types.PageBlockTable)]
    assert len(tables) == 1
    rows = tables[0].rows
    assert len(rows) == 5
    assert rows[0].cells[0].header is True
    section_rows = [row for row in rows[1:] if len(row.cells) == 1]
    assert len(section_rows) == 2
    for row in section_rows:
        assert row.cells[0].header is True
        assert row.cells[0].colspan == 2
    texts = []
    _plain_texts(rich.to_dict(), texts)
    assert "Start" in texts and "Controls" in texts
    data_rows = [row for row in rows[1:] if len(row.cells) == 2]
    assert len(data_rows) == 2
    assert all(cell.header is not True for row in data_rows for cell in row.cells)


def test_double_underscore_italic_is_absorbed_in_block_cards():
    from telethon import types

    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message(
        "## Search\n\n__No task list has been returned by the agent yet.__\n\n"
        "```\ncode\n```\n\n__To cancel, send `/stop`.__"
    )
    assert isinstance(rich, types.InputRichMessage)
    dump = rich.to_dict()
    italics = []

    def collect(node):
        if isinstance(node, dict):
            if node.get("_") == "TextItalic":
                texts = []
                _plain_texts(node, texts)
                italics.append("".join(texts))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(dump)
    assert "No task list has been returned by the agent yet." in italics
    assert any("To cancel, send " in item and "/stop" in item for item in italics)
    plains = []
    _plain_texts(dump, plains)
    assert not any("__" in text for text in plains)


def test_dash_bullets_become_a_native_list():
    from telethon import types

    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    rich = input_rich_message("- Max turns: **150** agent turns maximum\n- Depth: **Deep**\n",
                              rich_format="blocks")
    assert isinstance(rich, types.InputRichMessage)
    lists = [block for block in rich.blocks if isinstance(block, types.PageBlockList)]
    assert len(lists) == 1
    assert len(lists[0].items) == 2
    texts = []
    _plain_texts(rich.to_dict(), texts)
    assert "Max turns: 150 agent turns maximum" in "".join(texts)
