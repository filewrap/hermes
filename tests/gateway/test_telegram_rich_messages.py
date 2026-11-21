"""Regression guards for Telegram's MTProto-only Rich Message transport."""
from pathlib import Path

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import _build_adapter
from plugins.platforms.telegram.mtproto_rich import edit_request, input_rich_message, send_request
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _walk(node):
    """Walk Telethon TL objects/lists without relying on their repr formatting."""
    from telethon import types

    yield node
    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk(child)
    elif isinstance(node, types.TLObject):
        for child in vars(node).values():
            if isinstance(child, (types.TLObject, list, tuple)):
                yield from _walk(child)


def test_registered_telegram_adapter_is_telethon_mtproto():
    adapter = _build_adapter(PlatformConfig(extra={}))
    assert isinstance(adapter, TelethonAdapter)
    assert adapter.name == "Telegram (MTProto)"


def test_telegram_source_has_no_bot_api_rich_message_endpoint_wiring():
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "plugins/platforms/telegram/adapter.py",
        "plugins/platforms/telegram/telethon_adapter.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "sendRichMessage" not in source
        assert "sendRichMessageDraft" not in source
        assert "_bot_api_rich_fallback" not in source


def test_display_latex_is_sent_as_native_page_block_math_with_surrounding_blocks():
    from telethon import types

    source = (
        "## Derivation\n\n"
        "For **x**, the integral is:\n\n"
        "$$\n\\int_0^1 x^2\\,dx=\\frac13\n$$\n\n"
        "- First step\n- Final result"
    )
    request = send_request(types.PeerUser(1), source)

    assert isinstance(request.rich_message, types.InputRichMessage)
    blocks = request.rich_message.blocks
    assert [type(block).__name__ for block in blocks] == [
        "PageBlockHeading2", "PageBlockParagraph", "PageBlockMath", "PageBlockList",
    ]
    assert blocks[2].source == r"\int_0^1 x^2\,dx=\frac13"
    assert len(request._bytes()) > 0  # The MTProto request serializes with native blocks.


def test_inline_latex_uses_native_text_math_and_retains_markdown_emphasis():
    from telethon import types

    request = send_request(types.PeerUser(1), "The **answer** is $x^2 + y^2$. ")
    assert isinstance(request.rich_message, types.InputRichMessage)
    nodes = list(_walk(request.rich_message))
    assert any(isinstance(node, types.TextMath) and node.source == "x^2 + y^2" for node in nodes)
    assert any(isinstance(node, types.TextBold) for node in nodes)
    assert len(request._bytes()) > 0


def test_math_fence_is_native_page_block_and_dollars_in_code_are_not_math():
    from telethon import types

    request = send_request(types.PeerUser(1), "```latex\n\\sum_{n=1}^N n\n```")
    assert isinstance(request.rich_message, types.InputRichMessage)
    assert len(request.rich_message.blocks) == 1
    assert isinstance(request.rich_message.blocks[0], types.PageBlockMath)
    assert request.rich_message.blocks[0].source == r"\sum_{n=1}^N n"

    code = "Example: `$x$`\n\n```python\nprint('$$x$$')\n```"
    code_request = send_request(types.PeerUser(1), code)
    assert isinstance(code_request.rich_message, types.InputRichMessage)
    assert any(isinstance(block, types.PageBlockPreformatted) for block in code_request.rich_message.blocks)
    copy_row = next(block for block in code_request.rich_message.blocks
                    if isinstance(block, types.PageBlockButtonRow))
    assert isinstance(copy_row.buttons[0].type, types.InlineButtonTypeCopy)
    assert copy_row.buttons[0].type.copy_text == "print('$$x$$')"
    inline_copy = next(node for node in _walk(code_request.rich_message)
                       if isinstance(node, types.TextButton))
    assert isinstance(inline_copy.type, types.InlineButtonTypeCopy)
    assert inline_copy.type.copy_text == "$x$"
    assert len(code_request._bytes()) > 0


def test_markdown_hrefs_and_command_code_use_native_rich_link_and_copy_actions():
    from telethon import types

    source = "Open [the guide](https://example.com/docs), then run `/start`.\n\n```bash\necho ready\n```"
    request = send_request(types.PeerUser(1), source)
    nodes = list(_walk(request.rich_message))
    assert any(isinstance(node, types.TextUrl) and node.url == "https://example.com/docs" for node in nodes)
    assert any(isinstance(node, types.TextButton) and isinstance(node.type, types.InlineButtonTypeCopy)
               and node.type.copy_text == "/start" for node in nodes)
    assert any(isinstance(node, types.PageButton) and isinstance(node.type, types.InlineButtonTypeCopy)
               and node.type.copy_text == "echo ready" for node in nodes)
    assert len(request._bytes()) > 0


def test_fenced_code_copy_pagebutton_boundary_is_100_characters():
    from telethon import types

    for size, expects_copy in ((100, True), (101, False)):
        code = "x" * size
        request = send_request(types.PeerUser(1), f"```text\n{code}\n```")
        assert isinstance(request.rich_message, types.InputRichMessage)
        pre = next(block for block in request.rich_message.blocks
                   if isinstance(block, types.PageBlockPreformatted))
        assert pre.text.text == code
        copy_rows = [block for block in request.rich_message.blocks
                     if isinstance(block, types.PageBlockButtonRow)
                     and any(isinstance(button.type, types.InlineButtonTypeCopy)
                             for button in block.buttons)]
        assert bool(copy_rows) is expects_copy
        if expects_copy:
            assert copy_rows[0].buttons[0].type.copy_text == code
        assert len(request._bytes()) > 0


def test_inline_code_copy_textbutton_boundary_is_100_characters():
    from telethon import types

    for size, expects_copy in ((100, True), (101, False)):
        code = "x" * size
        request = send_request(types.PeerUser(1), f"`{code}`")
        inline_buttons = [node for node in _walk(request.rich_message)
                          if isinstance(node, types.TextButton)]
        copies = [node for node in inline_buttons
                  if isinstance(node.type, types.InlineButtonTypeCopy)]
        assert bool(copies) is expects_copy
        if expects_copy:
            assert copies[0].type.copy_text == code
        else:
            assert not inline_buttons
            assert any(isinstance(node, types.TextFixed) for node in _walk(request.rich_message))
        assert len(request._bytes()) > 0


def test_markdown_html_send_and_edit_remain_native_rich_message_variants():
    from telethon import types

    markdown = send_request(types.PeerUser(1), "**Strong** and _emphasized_.")
    assert isinstance(markdown.rich_message, types.InputRichMessageMarkdown)
    assert len(markdown._bytes()) > 0

    html_source = "<table><tr><th>Term</th><th>Value</th></tr><tr><td>x</td><td>2</td></tr></table>"
    html = send_request(types.PeerUser(1), html_source)
    assert isinstance(html.rich_message, types.InputRichMessageHTML)
    assert html.rich_message.html == html_source
    assert len(html._bytes()) > 0
    html_link_source = '<p>Open <a href="https://example.com/docs">the guide</a>.</p>'
    html_link = send_request(types.PeerUser(1), html_link_source, rich_format="html")
    assert isinstance(html_link.rich_message, types.InputRichMessageHTML)
    assert html_link.rich_message.html == html_link_source
    assert len(html_link._bytes()) > 0

    edited = edit_request(types.PeerUser(1), 17, "$$x^2$$")
    assert isinstance(edited.rich_message, types.InputRichMessage)
    assert isinstance(edited.rich_message.blocks[0], types.PageBlockMath)
    assert len(edited._bytes()) > 0


def test_auto_detects_embedded_html_after_prose_but_keeps_code_examples_literal():
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import send_request

    html_source = 'Status: <b>ready</b>; style: <i>native</i>. <a href="https://example.com">Open</a>'
    html = send_request(types.PeerUser(1), html_source)
    assert isinstance(html.rich_message, types.InputRichMessageHTML)
    assert html.rich_message.html == html_source
    assert html.message == html_source
    assert len(html._bytes()) > 0

    code_example = "Literal tags:\n```html\n<b>ready</b>\n```"
    code = send_request(types.PeerUser(1), code_example)
    assert not isinstance(code.rich_message, types.InputRichMessageHTML)
    assert isinstance(code.rich_message, types.InputRichMessage)


def test_in_body_rich_buttons_render_and_validate_server_limits():
    import pytest
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import (
        rich_button, rich_button_row, rich_page_footer, send_request,
    )

    assert rich_button("Docs", kind="url", value="https://example.com", style="primary") == (
        '<tg-button type="url" style="primary" url="https://example.com">Docs</tg-button>')
    assert rich_button("Copy", kind="copy", value="clipboard") == (
        '<tg-button type="copy_text" text="clipboard">Copy</tg-button>')
    assert rich_button("Member", kind="profile", value="8") == (
        '<tg-button type="url" url="tg://user?id=8">Member</tg-button>')
    assert rich_button("Details", kind="callback", value="ps:1:home", style="link") == (
        '<tg-button type="callback_data" style="link" data="ps:1:home">Details</tg-button>')
    assert rich_button("Locked", kind="disabled", style="primary") == (
        '<tg-button type="disabled" style="primary">Locked</tg-button>')
    assert rich_button("A & <B>", kind="url", value="https://example.com") == (
        '<tg-button type="url" url="https://example.com">A &amp; &lt;B&gt;</tg-button>')
    assert rich_button_row([rich_button("One", kind="callback", value="x")], align="center") == (
        '<tg-button-row align="center">'
        '<tg-button type="callback_data" data="x">One</tg-button></tg-button-row>')
    assert rich_page_footer(2, 3, 41) == "<p><i>Page 2 of 3 • total 41</i></p>"
    with pytest.raises(ValueError, match="Callback rich buttons"):
        rich_button("Go", kind="callback", value="x" * 65)
    with pytest.raises(ValueError, match="Copy rich buttons"):
        rich_button("Copy", kind="copy", value="x" * 257)
    with pytest.raises(ValueError, match="link button style"):
        rich_button("Open", kind="url", value="https://example.com", style="link")
    with pytest.raises(ValueError, match="numeric Telegram user ID"):
        rich_button("Member", kind="profile", value="not-a-user")
    with pytest.raises(ValueError, match="WebView|kind must be"):
        rich_button("App", kind="webview", value="https://example.com")
    with pytest.raises(ValueError, match="1–8 buttons"):
        rich_button_row([])
    with pytest.raises(ValueError, match="Page indexation"):
        rich_page_footer(0, 1, 1)

    html_source = ('<p>Read the guide.</p><tg-button-row>'
                   '<tg-button type="callback_data" data="ps:1:home">Open</tg-button></tg-button-row>')
    sent = send_request(types.PeerUser(1), html_source)
    assert isinstance(sent.rich_message, types.InputRichMessageHTML)
    assert sent.rich_message.html == html_source
    assert len(sent._bytes()) > 0


def test_rich_markdown_normalizer_preserves_math_and_indented_list_structure():
    from plugins.platforms.telegram.mtproto_rich import normalize_rich_markdown

    source = "Result:\n\n$$\nx^2 + y^2\n=z^2\n$$\n\n- parent\n  - nested\n1. ordered\n   continuation\n"
    normalized = normalize_rich_markdown(source)
    assert "$$\nx^2 + y^2\n=z^2\n$$" in normalized
    assert "- parent\n  - nested\n1. ordered\n   continuation" in normalized


def test_rich_message_chunking_respects_native_limit_without_splitting_formula_blocks():
    from plugins.platforms.telegram.mtproto_rich import MAX_RICH_MESSAGE_CHARS, split_rich_message

    formula = "$$\n\\sum_{n=1}^{10} n\n$$"
    parts = split_rich_message("p" * 32_740 + "\n\n" + formula + "\n\nConclusion")

    assert len(parts) == 2
    assert all(len(part) <= MAX_RICH_MESSAGE_CHARS for part in parts)
    assert "$$" not in parts[0]
    assert formula in parts[1]
    assert parts[0].endswith("(1/2)")
    assert parts[1].endswith("(2/2)")


def test_math_rich_blocks_preserve_paragraphs_nested_list_indentation_and_tables():
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import send_request

    message = (
        "## Result\n\n"
        "Inline formula: \\(a^2+b^2=c^2\\).\n\n"
        "$$\n\\int_0^1 x\\,dx=\\frac12\n$$\n\n"
        "- Parent item\n  - Child item\n    - Grandchild item\n\n"
        "| Name | Value |\n|:-----|------:|\n| Answer | **42** |"
    )
    request = send_request(types.PeerUser(1), message)

    assert isinstance(request.rich_message, types.InputRichMessage)
    assert [type(block).__name__ for block in request.rich_message.blocks] == [
        "PageBlockHeading2", "PageBlockParagraph", "PageBlockMath",
        "PageBlockList", "PageBlockTable",
    ]
    parent_item = request.rich_message.blocks[3].items[0]
    assert type(parent_item).__name__ == "PageListItemBlocks"
    child_list = parent_item.blocks[1]
    assert type(child_list).__name__ == "PageBlockList"
    child_item = child_list.items[0]
    assert type(child_item).__name__ == "PageListItemBlocks"
    assert type(child_item.blocks[1]).__name__ == "PageBlockList"
    math_nodes = [node for node in _walk(request.rich_message) if isinstance(node, types.TextMath)]
    assert len(math_nodes) == 1 and math_nodes[0].source == "a^2+b^2=c^2"
    assert len(request._bytes()) > 0


def test_explicit_math_blocks_enforce_native_structural_limits():
    import pytest
    from telethon import types
    from plugins.platforms.telegram.mtproto_rich import send_request

    columns = [f"C{i}" for i in range(21)]
    table = "| " + " | ".join(columns) + " |\n| " + " | ".join(["---"] * 21) + " |"
    with pytest.raises(ValueError, match="20 columns"):
        send_request(types.PeerUser(1), "$x$\n\n" + table)

    nested = "\n".join("  " * depth + f"- Level {depth}" for depth in range(17))
    with pytest.raises(ValueError, match="16 nested levels"):
        send_request(types.PeerUser(1), nested + "\n\n$x$")

    many_items = "\n".join(f"- Item {index}" for index in range(500))
    with pytest.raises(ValueError, match="500 blocks"):
        send_request(types.PeerUser(1), many_items + "\n\n$x$")
