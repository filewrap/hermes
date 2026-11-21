"""Native MTProto Rich Message Markdown must preserve intended line breaks."""
import pytest
from telethon import types

from plugins.platforms.telegram.mtproto_rich import edit_request, send_request


@pytest.mark.parametrize("builder", [
    lambda text: send_request(types.PeerUser(1), text),
    lambda text: edit_request(types.PeerUser(1), 2, text),
])
def test_single_newlines_become_hard_breaks_in_native_rich_payload(builder):
    content = "Line 1\nLine 2\nLine 3"
    expected = "Line 1  \nLine 2  \nLine 3"
    request = builder(content)

    assert request.message == expected
    assert request.rich_message.markdown == expected
    assert request._bytes()


def test_paragraph_breaks_are_preserved():
    content = "Paragraph 1\n\nParagraph 2"
    request = send_request(types.PeerUser(1), content)
    assert request.rich_message.markdown == content


def test_tables_and_fenced_code_keep_their_internal_newlines():
    table = "| Col A | Col B |\n|---|---|\n| 1 | 2 |"
    fenced = "Before\n```py\nprint(1)\nprint(2)\n```\nAfter"
    table_request = send_request(types.PeerUser(1), table)
    fenced_request = send_request(types.PeerUser(1), fenced)

    assert table_request.rich_message.markdown == table
    # A real fenced block is copyable code, so it correctly routes to the
    # blocks payload: the test's intent (internal newlines survive) is
    # asserted on the structure the code actually produces.
    assert isinstance(fenced_request.rich_message, types.InputRichMessage)
    blocks = fenced_request.rich_message.blocks
    preformatted = [block for block in blocks if isinstance(block, types.PageBlockPreformatted)]
    assert len(preformatted) == 1
    assert preformatted[0].text.text == "print(1)\nprint(2)"
    assert preformatted[0].language == "py"
    paragraphs = [block.text.text for block in blocks if isinstance(block, types.PageBlockParagraph)]
    assert paragraphs == ["Before", "After"]
    copy_rows = [block for block in blocks if isinstance(block, types.PageBlockButtonRow)]
    assert len(copy_rows) == 1
    assert copy_rows[0].buttons[0].type.copy_text == "print(1)\nprint(2)"
