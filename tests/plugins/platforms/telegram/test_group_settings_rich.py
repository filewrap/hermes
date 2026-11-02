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
        {"allow_all": True, "trigger_mode": "tag", "allowed_users": {}},
        chat_name="Project group",
        bot_username="HermesBot",
    )

    assert text.startswith("<h1>Group settings · Project group</h1>")
    assert "<blockquote><h2>ALLOW ALL · ON</h2>" in text
    assert text.index("ALLOW ALL · ON") < text.index("<h2>Invocation</h2>")
    assert "<details><summary>Access and privacy policy</summary>" in text
    assert "Messages that do not meet the selected trigger are silently ignored" in text
    assert "or reply to one of this bot's messages" in text

    rich = input_rich_message(text)
    assert isinstance(rich, types.InputRichMessageHTML)
    assert rich.html.endswith("<p><i>Page 1 of 1 • total 1</i></p>")
    assert rich.html.count("Page 1 of 1") == 1
    assert rich._bytes()  # Telethon can serialize the exact native HTML constructor.
    buttons = {button.text: button for button in _buttons(markup)}
    assert buttons["Allow all · ON"].style.bg_success is True
    assert not buttons["Allow all · OFF"].style.bg_danger
    assert not any("✓" in button.text or "✔" in button.text for button in _buttons(markup))


def test_group_settings_off_restricts_guests_and_colors_selected_choices():
    text, markup = settings_view(
        "forum",
        {"allow_all": False, "trigger_mode": "trigger", "allowed_users": {}},
        bot_name="Hermes",
    )
    assert "ALLOW ALL · OFF" in text
    assert "Only the owner and explicitly authorized users can use Hermes here" in text
    assert "Unlisted guests cannot gain access through a name prefix" in text
    assert "Allow all changes only guest access in this group" in text
    assert "does not change global grants, direct-message access, or tool permissions" in text
    assert "a reply to one of its messages is only an invocation trigger, not guest authorization" in text

    buttons = {button.text: button for button in _buttons(markup)}
    assert not buttons["Allow all · ON"].style.bg_success
    assert buttons["Allow all · OFF"].style.bg_danger is True
    assert buttons["Name trigger"].style.bg_success is True
    assert not any("✓" in button.text or "✔" in button.text for button in _buttons(markup))


def test_dynamic_group_settings_values_are_html_escaped():
    text, _ = settings_view(
        "group",
        {
            "allow_all": True,
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
    group_text, group_markup = settings_view("group", {"allow_all": False})
    forum_text, forum_markup = settings_view("forum", {"allow_all": False})
    channel_text, channel_markup = settings_view("channel", {"allow_all": True})
    dm_text, dm_markup = settings_view("dm", {"allowed_users": {"7": {"name": "Guest"}}})

    assert "ALLOW ALL · OFF" in group_text and "ALLOW ALL · OFF" in forum_text
    assert any(button.type.data == b"hm:settings:allowall:on" for button in _buttons(group_markup))
    assert any(button.type.data == b"hm:settings:allowall:on" for button in _buttons(forum_markup))
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
