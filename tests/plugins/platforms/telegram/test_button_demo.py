from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import functions, types

from gateway.config import PlatformConfig
from plugins.platforms.telegram.button_demo import (
    CALLBACK_PREFIX,
    build_button_gallery,
    callback_acknowledgement,
    handle_reply_keyboard_text,
    show_button_gallery,
)
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


def _walk(nodes):
    stack = list(nodes) if isinstance(nodes, (list, tuple)) else [nodes]
    while stack:
        node = stack.pop()
        if isinstance(node, types.TLObject):
            yield node
            for value in vars(node).values():
                if isinstance(value, (list, tuple)):
                    stack.extend(value)
                elif isinstance(value, types.TLObject):
                    stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)


def _color_flags(style):
    return [name for name in ("bg_primary", "bg_success", "bg_danger") if getattr(style, name, False)]


def _callback_data(button):
    assert isinstance(button.type, types.InlineButtonTypeCallback)
    return button.type.data.decode("ascii")


def test_gallery_is_a_real_native_richmessage_block_tree_not_a_button_grid():
    rich_message, inline_markup, reply_markup = build_button_gallery()

    assert isinstance(rich_message, types.InputRichMessage)
    assert bytes(rich_message)
    nodes = list(_walk(rich_message))
    block_types = {type(node) for node in nodes}
    assert isinstance(rich_message.blocks[0], types.PageBlockHeading1)
    assert not any(isinstance(node, types.PageBlockTitle) for node in nodes)
    for block_type in (
        types.PageBlockHeading1,
        types.PageBlockHeading2,
        types.PageBlockHeading3,
        types.PageBlockParagraph,
        types.PageBlockDivider,
        types.PageBlockTable,
        types.PageBlockBlockquote,
        types.PageBlockPullquote,
        types.PageBlockList,
        types.PageBlockPreformatted,
        types.PageBlockMath,
        types.PageBlockDetails,
        types.PageBlockButtonRow,
        types.PageBlockFooter,
    ):
        assert block_type in block_types

    table = next(node for node in nodes if isinstance(node, types.PageBlockTable))
    assert len(table.rows) == 4
    assert [cell.text.text for cell in table.rows[0].cells] == [
        "Feature", "Native block", "Example",
    ]
    assert sum(isinstance(node, types.PageBlockList) for node in nodes) >= 2
    tasks = [node for node in nodes if isinstance(node, types.PageListItemText) and node.checkbox]
    assert {(task.checked, task.text.text) for task in tasks} == {
        (True, "Completed task"), (False, "Pending task"),
    }
    assert any(isinstance(node, types.PageBlockPreformatted) and node.language == "python" for node in nodes)
    assert any(isinstance(node, types.PageBlockMath) and "E = mc^2" in node.source for node in nodes)
    assert any(isinstance(node, types.TextMath) and node.source == "x^2 + y^2" for node in nodes)
    assert any(isinstance(node, types.TextBold) for node in nodes)
    assert any(isinstance(node, types.TextItalic) for node in nodes)
    assert any(isinstance(node, types.TextFixed) for node in nodes)
    assert any(isinstance(node, types.TextUrl) for node in nodes)

    standard_quote = next(node for node in nodes if isinstance(node, types.PageBlockBlockquote))
    assert "regular block quotation" in standard_quote.text.text
    pull_quote = next(node for node in nodes if isinstance(node, types.PageBlockPullquote))
    assert "distinct native block" in pull_quote.text.text

    details = [node for node in nodes if isinstance(node, types.PageBlockDetails)]
    nested = next(section for section in details if section.title.text == "Expand for nested rich buttons")
    nested_rows = [node for node in _walk(nested.blocks) if isinstance(node, types.PageBlockButtonRow)]
    assert len(nested_rows) == 1 and len(nested_rows[0].buttons) == 2

    rich_buttons = [node for node in nodes if isinstance(node, types.PageButton)]
    assert rich_buttons
    assert any(isinstance(node, types.TextButton) for node in nodes)
    callback_buttons = [button for button in rich_buttons
                        if isinstance(button.type, types.InlineButtonTypeCallback)]
    assert callback_buttons
    assert all(_callback_data(button).startswith(CALLBACK_PREFIX) for button in callback_buttons)
    copy_buttons = [button for button in rich_buttons
                    if isinstance(button.type, types.InlineButtonTypeCopy)]
    assert copy_buttons
    assert copy_buttons[0].type.copy_text == 'from telethon import types\nprint("native RichMessage blocks")'
    inline_copy = [node for node in nodes if isinstance(node, types.TextButton)
                   and isinstance(node.type, types.InlineButtonTypeCopy)]
    assert any(button.type.copy_text == "fixed-width code" for button in inline_copy)
    assert any(isinstance(node, types.RichButtonStyle) and node.link for node in nodes)
    colored_rich = [button.style for button in rich_buttons if button.style is not None]
    assert any(_color_flags(style) == ["bg_primary"] for style in colored_rich)
    assert any(_color_flags(style) == ["bg_success"] for style in colored_rich)
    assert any(_color_flags(style) == ["bg_danger"] for style in colored_rich)

    limitation = next(section for section in details if "limitation" in section.title.text)
    limitation_text = limitation.blocks[0].text.text
    assert "real Telegram media references" in limitation_text
    assert "does not fabricate" in limitation_text

    # Table/task-list syntax was parsed into TL blocks; it is not leaked as literal markup.
    paragraph_text = [node.text.text for node in nodes if isinstance(node, types.PageBlockParagraph)
                      and isinstance(node.text, types.TextPlain)]
    assert not any("| Feature |" in text or "- [x]" in text for text in paragraph_text)
    assert isinstance(inline_markup, types.ReplyInlineMarkup)
    assert isinstance(reply_markup, types.ReplyKeyboardMarkup)


def test_regular_inline_keyboard_uses_neutral_and_native_colored_callback_buttons():
    _rich_message, markup, _reply_markup = build_button_gallery()

    assert isinstance(markup, types.ReplyInlineMarkup)
    assert [len(row.buttons) for row in markup.rows] == [2, 2]
    buttons = [button for row in markup.rows for button in row.buttons]
    assert all(isinstance(button, types.KeyboardInlineButton) for button in buttons)
    assert buttons[0].style is None
    assert [_color_flags(button.style) for button in buttons[1:]] == [
        ["bg_primary"], ["bg_success"], ["bg_danger"],
    ]
    payloads = [_callback_data(button) for button in buttons]
    assert len(payloads) == len(set(payloads))
    assert all(payload.startswith(CALLBACK_PREFIX) for payload in payloads)
    assert bytes(markup)


def test_reply_keyboard_is_distinct_styled_text_markup_and_single_use():
    _rich_message, _inline_markup, markup = build_button_gallery()

    assert isinstance(markup, types.ReplyKeyboardMarkup)
    assert markup.single_use is True
    assert not markup.persistent
    assert [len(row.buttons) for row in markup.rows] == [1, 3]
    buttons = [button for row in markup.rows for button in row.buttons]
    assert all(isinstance(button, types.KeyboardButton) for button in buttons)
    assert all(isinstance(button.type, types.ButtonTypeDefault) for button in buttons)
    assert [button.text for button in buttons] == [
        "Reply neutral", "Reply blue", "Reply green", "Reply red",
    ]
    assert buttons[0].style is None
    assert [_color_flags(button.style) for button in buttons[1:]] == [
        ["bg_primary"], ["bg_success"], ["bg_danger"],
    ]
    assert bytes(markup)


@pytest.mark.asyncio
async def test_group_reply_button_tap_clears_chat_keyboard_state_and_sends_only_a_rich_ack():
    adapter = SimpleNamespace(
        _button_demo_reply_keyboards={("-10077", "100"): time.monotonic()},
        _authorized=Mock(return_value=True),
        _peer_for=Mock(return_value=types.InputPeerChannel(channel_id=77, access_hash=0)),
        _mtproto_rich_request=AsyncMock(return_value=SimpleNamespace(id=903)),
    )
    source = SimpleNamespace(chat_id="-10077", user_id="201", thread_id=None)

    consumed = await handle_reply_keyboard_text(adapter, source, "Reply green")

    assert consumed is True
    assert not adapter._button_demo_reply_keyboards
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert isinstance(request.rich_message, types.InputRichMessage)
    assert isinstance(request.reply_markup, types.ReplyKeyboardHide)
    assert bytes(request)


def test_gallery_is_listed_as_a_telegram_only_no_argument_command():
    from hermes_cli.commands_platforms import telegram_bot_commands
    from plugins.platforms.telegram.rich_ui import help_sections

    assert "button" in dict(telegram_bot_commands(include_plugins=False))
    entries = [entry for _category, commands in help_sections(skill_commands={}) for entry in commands]
    assert ("/button", "Show the native Telegram RichMessage and button styles") in entries


@pytest.mark.asyncio
async def test_gallery_sends_native_rich_message_and_separate_inline_and_reply_markup_requests():
    adapter = SimpleNamespace(
        _peer_for=Mock(return_value=types.InputPeerUser(user_id=100, access_hash=0)),
        _mtproto_rich_request=AsyncMock(side_effect=[SimpleNamespace(id=901), SimpleNamespace(id=902)]),
        _authorized=Mock(return_value=True),
    )
    source = SimpleNamespace(chat_id="100", user_id="100", thread_id="77")

    result = await show_button_gallery(adapter, source, reply_to=88)

    assert result.id == 901
    assert adapter._mtproto_rich_request.await_count == 2
    rich_request, reply_keyboard_request = [
        call.args[0] for call in adapter._mtproto_rich_request.await_args_list
    ]
    assert isinstance(rich_request, functions.messages.SendMessageRequest)
    assert isinstance(rich_request.rich_message, types.InputRichMessage)
    assert isinstance(rich_request.reply_markup, types.ReplyInlineMarkup)
    assert rich_request.reply_to.reply_to_msg_id == 88
    assert rich_request.reply_to.top_msg_id == 77
    assert bytes(rich_request)

    assert isinstance(reply_keyboard_request, functions.messages.SendMessageRequest)
    assert isinstance(reply_keyboard_request.rich_message, types.InputRichMessage)
    assert isinstance(reply_keyboard_request.reply_markup, types.ReplyKeyboardMarkup)
    assert reply_keyboard_request.reply_markup.single_use is True
    assert reply_keyboard_request.reply_to.reply_to_msg_id == 901
    assert reply_keyboard_request.reply_to.top_msg_id == 77
    assert bytes(reply_keyboard_request)
    assert adapter._button_demo_reply_keyboards[("100", "100")]


@pytest.mark.asyncio
async def test_button_callback_only_answers_with_safe_acknowledgement_and_never_routes_to_agent():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._source = AsyncMock(return_value=SimpleNamespace(
        chat_type="dm", chat_id="100", user_id="100", is_bot=False,
    ))
    adapter._authorized = Mock(return_value=True)
    adapter.handle_message = AsyncMock()
    event = SimpleNamespace(data=b"hb:v1:rich-nested-green", answer=AsyncMock())

    await adapter._on_callback_query(event)

    event.answer.assert_awaited_once_with("Button demo: RichMessage · nested green button")
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_button_callback_rechecks_authorization_before_acknowledging():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._source = AsyncMock(return_value=SimpleNamespace(
        chat_type="dm", chat_id="100", user_id="201", is_bot=False,
    ))
    adapter._authorized = Mock(return_value=False)
    adapter.handle_message = AsyncMock()
    event = SimpleNamespace(data=b"hb:v1:inline-blue", answer=AsyncMock())

    await adapter._on_callback_query(event)

    event.answer.assert_awaited_once_with(
        "This action is not available for this account.", alert=True,
    )
    adapter.handle_message.assert_not_awaited()


def test_unknown_demo_callback_cannot_be_mapped_to_an_action():
    assert callback_acknowledgement("hb:v1:not-a-gallery-action") is None
    assert callback_acknowledgement("hm:settings:save") is None
