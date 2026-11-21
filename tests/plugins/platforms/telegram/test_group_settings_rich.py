from telethon import types

from plugins.platforms.telegram.mtproto_rich import input_rich_message
from plugins.platforms.telegram.telegram_chat_settings import (
    allow_confirmation_view,
    settings_view,
)


def _buttons(markup):
    return [button for row in markup.rows for button in row.buttons]


def test_group_settings_use_structured_native_rich_html_with_priority_panel():
    text, markup = settings_view(
        "group",
        {"trigger_word": "naina", "trigger_mode": "tag", "allowed_users": {}},
        chat_name="Project group",
        bot_username="HermesBot",
    )

    assert text.startswith("<h1>Group settings · Project group</h1>")
    assert "<h2>Invocation</h2>" in text
    assert "ALLOW ALL" not in text
    assert "Custom trigger word: <b>naina</b>" in text
    assert "Messages that do not meet the selected trigger are silently ignored" in text
    assert "or reply to one of this bot's messages" in text

    rich = input_rich_message(text)
    assert isinstance(rich, types.InputRichMessageHTML)
    assert rich.html.endswith("<p><i>Page 1 of 1 • total 1</i></p>")
    assert rich.html.count("Page 1 of 1") == 1
    assert rich._bytes()  # Telethon can serialize the exact native HTML constructor.
    buttons = {button.text: button for button in _buttons(markup)}
    assert "Username" in buttons
    assert buttons["Username"].style.bg_success is True
    assert not any("✓" in button.text or "✔" in button.text for button in _buttons(markup))


def test_group_settings_off_restricts_guests_and_colors_selected_choices():
    text, markup = settings_view(
        "forum",
        {"trigger_word": "", "trigger_mode": "trigger", "allowed_users": {}},
        bot_name="Hermes",
    )
    assert "ALLOW ALL" not in text
    assert "a reply to one of its messages is only an invocation trigger, not guest authorization" in text
    assert "The owner and explicitly authorized users remain governed by their normal access" in text

    buttons = {button.text: button for button in _buttons(markup)}
    assert buttons["Name trigger"].style.bg_success is True
    assert not any("✓" in button.text or "✔" in button.text for button in _buttons(markup))


def test_dynamic_group_settings_values_are_html_escaped():
    text, _ = settings_view(
        "group",
        {
            "trigger_word": "naina",
            "trigger_mode": "trigger",
            "allowed_users": {
                '42<&"': {
                    "id": '42<&"',
                    "name": '<b>Alice & Bob</b>',
                    "username": 'alice<&"',
                }
            },
        },
        chat_name='<img src="x"> & team',
        bot_name='<script>Hermes</script>',
        bot_username='bot<&"',
        note='<b>Updated & saved</b>',
    )

    assert '&lt;img src=&quot;x&quot;&gt; &amp; team' in text
    assert '&lt;script&gt;Hermes&lt;/script&gt;' in text
    assert "Alice &amp; Bob" not in text
    assert '42&lt;&amp;&quot;' not in text
    assert 'alice&lt;&amp;&quot;' not in text
    assert '&lt;b&gt;Updated &amp; saved&lt;/b&gt;' in text
    assert '<img src="x">' not in text
    assert '<script>' not in text
    assert '<b>Alice' not in text


def test_group_only_controls_and_existing_channel_dm_and_confirmation_views_remain():
    group_text, group_markup = settings_view("group", {"trigger_word": "naina"})
    forum_text, forum_markup = settings_view("forum", {"trigger_word": ""})
    channel_text, channel_markup = settings_view("channel", {"trigger_word": ""})
    dm_text, dm_markup = settings_view("dm", {"allowed_users": {"7": {"name": "Guest"}}})

    assert "Custom trigger word: <b>naina</b>" in group_text
    assert "Custom trigger word" not in forum_text
    assert "ALLOW ALL" not in group_text and "ALLOW ALL" not in forum_text
    assert all(not button.type.data.startswith(b"hm:settings:allowall:") for button in _buttons(group_markup))
    assert all(not button.type.data.startswith(b"hm:settings:allowall:") for button in _buttons(forum_markup))
    assert "ALLOW ALL" not in channel_text and "ALLOW ALL" not in dm_text
    assert all(not button.type.data.startswith(b"hm:settings:allowall:") for button in _buttons(channel_markup))
    assert all(not button.type.data.startswith(b"hm:settings:mode:") for button in _buttons(dm_markup))
    assert "Channel settings" in channel_text
    assert "owner-only <code>/allowlist</code>" in dm_text
    assert "Guest" not in dm_text and "<code>7</code>" not in dm_text

    confirmation, confirm_markup = allow_confirmation_view(
        {"id": "7", "name": "Guest", "username": "guest"}, "target group", "token"
    )
    assert "Confirm access grant" in confirmation
    assert any(button.type.data == b"hm:allow:confirm:token" for button in _buttons(confirm_markup))


def test_group_settings_triggers_row_opens_triggers_card():
    text, _ = settings_view(
        "group",
        {"trigger_word": "word", "trigger_mode": "trigger", "allowed_users": {}},
        chat_name="Project group",
        bot_username="HermesBot",
    )
    assert "<td><b>Triggers</b></td><td>word</td>" in text
    assert 'data="hm:settings:triggers:open"' in text
    assert ">Open</tg-button>" in text
    rich = input_rich_message(text)
    assert isinstance(rich, types.InputRichMessageHTML)
    assert rich._bytes()  # The table row serializes as native RichMessage HTML.

    off_text, _ = settings_view(
        "forum",
        {"trigger_word": "", "trigger_mode": "trigger", "allowed_users": {}},
    )
    assert "<td><b>Triggers</b></td><td>off</td>" in off_text
    assert 'data="hm:settings:triggers:open"' in off_text

    dm_text, dm_markup = settings_view("dm", {"allowed_users": {}})
    assert "Triggers" not in dm_text
    assert not any(button.type.data.startswith(b"hm:settings:triggers:")
                   for button in _buttons(dm_markup))
    channel_text, _ = settings_view(
        "channel",
        {"trigger_word": "", "trigger_mode": "trigger", "allowed_users": {}},
    )
    assert "Triggers" not in channel_text
    assert "hm:settings:triggers:open" not in channel_text
