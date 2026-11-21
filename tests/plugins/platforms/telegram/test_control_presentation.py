"""Delegated setup cards retain native HTML and one final page footer."""
from telethon import types

from plugins.platforms.telegram.control_html import setup_card_html
from plugins.platforms.telegram.control_navigation import ControlNavigation


def test_markdown_delegation_is_html_and_footer_follows_status():
    rich = types.InputRichMessageMarkdown(
        "# Preferences\n\n| Setting | Value |\n| --- | --- |\n| Memory | On |\n\n"
        "_Page 2 of 3 • total 14_\n\nSaved successfully."
    )
    converted, text = setup_card_html(rich, rich.markdown, 6)
    assert isinstance(converted, types.InputRichMessageHTML)
    assert "<table>" in text and "<h1>Preferences</h1>" in text
    assert text.count("Page 2 of 3 • total 14") == 1
    assert text.endswith("<p><em>Page 2 of 3 • total 14</em></p>")
    assert text.index("Saved successfully") < text.index("Page 2 of 3")


def test_html_footer_is_not_duplicated_on_repeated_route_conversion():
    rich = types.InputRichMessageHTML("<h1>Settings</h1>")
    first, _ = setup_card_html(rich, rich.html, 7)
    second, text = setup_card_html(first, first.html, 7)
    assert isinstance(second, types.InputRichMessageHTML)
    assert text.count("Page 1 of 1 • total 7") == 1
    assert text.endswith("<p><i>Page 1 of 1 • total 7</i></p>")


def test_history_is_bounded_deduplicated_and_copies_semantic_destinations():
    history = ControlNavigation(limit=2)
    origin = {"kind": "setup", "section": "home", "page": 3}
    history.push(origin)
    history.push(origin)
    origin["page"] = 1
    assert history.pop()["page"] == 3
    for page in (1, 2, 3):
        history.push({"kind": "setup", "section": "home", "page": page})
    assert len(history.frames) == 2
    assert [history.pop()["page"], history.pop()["page"]] == [3, 2]
    assert history.pop() is None
