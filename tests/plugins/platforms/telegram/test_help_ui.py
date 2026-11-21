"""Help-only RichMessage layout and private settings return navigation."""
from dataclasses import replace
import re
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from plugins.platforms.telegram import rich_ui
from plugins.platforms.telegram.mtproto_rich import send_request
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI
from tests.plugins.platforms.telegram.test_personal_settings_ui import (
    CallbackEvent, FakeAdapter, Source, _buttons_by_text,
)


@pytest.fixture
def sections(monkeypatch):
    catalog = [
        ("Session", [(f"/cmd{i} <arg>", f"Command {i} & details") for i in range(7)]),
        ("Info", [("/help", "Browse commands")]),
        ("Threads", [("/thread", "Manage threads")]),
        ("Skills", [("/research", "Research a topic")]),
    ]
    monkeypatch.setattr(rich_ui, "help_sections", lambda **_: catalog)
    return catalog


def html_body(text):
    return ET.fromstring(f"<body>{text}</body>")


@pytest.mark.parametrize("view,page,footer", [
    ("home", 0, "Page 1 of 1 • total 10"),
    ("overview", 9, "Page 1 of 1 • total 10"),
    ("0", 0, "Page 1 of 2 • total 7"),
    ("0", 1, "Page 2 of 2 • total 7"),
    ("0", 999, "Page 2 of 2 • total 7"),
    ("0", -1, "Page 1 of 2 • total 7"),
    ("0", "bad", "Page 1 of 2 • total 7"),
    ("skills", 0, "Page 1 of 1 • total 1"),
    ("missing", 0, "Page 1 of 1 • total 10"),
])
def test_help_native_html_hierarchy_and_final_italic_footer(sections, view, page, footer):
    text, markup = rich_ui.help_view(view, page)
    request = send_request("123", text, reply_markup=markup)
    assert request.rich_message.html.replace("\n", "") == text.replace("\n", "")
    children = list(html_body(text))
    assert [node.tag for node in children[:4]] == ["h1", "p", "hr", "h2"]
    assert children[0].text == "Hermes Help"
    assert children[-2].tag == "hr"
    assert children[-1].tag == "p"
    assert children[-1].find("i").text == footer
    assert len(list(html_body(text).iter("i"))) == 1
    assert "slash menu" not in text.lower()
    assert "100 commands" not in text
    assert "do not fit" not in text
    assert "capacity" not in text.lower()
    assert "<table" not in text
    assert request.reply_markup.rows


def test_help_compact_entries_escape_dynamic_data_and_preserve_navigation(sections):
    text, markup = rich_ui.help_view("0", 1)
    body = html_body(text)
    assert body.find("h2").text == "Session"
    assert [node.text for node in body.findall("ul/li/b/code")] == ["/cmd5 <arg>", "/cmd6 <arg>"]
    assert "Command 5 &amp; details" in text
    buttons = _buttons_by_text(markup)
    assert buttons["Personal settings"].type.data == b"hm:personal:open:0:1"
    assert buttons["‹ 1/2 · 7 total"].type.data == b"hm:help:0:0"
    assert not any(label.startswith("Next ") for label in buttons)
    assert max(len(button.type.data) for button in buttons.values()) <= 64
    home, home_markup = rich_ui.help_view()
    assert "<b>Gateway commands:</b> 9" in home
    assert "<b>Skill commands:</b> 1" in home
    assert _buttons_by_text(home_markup)["Personal settings"].type.data == b"hm:personal:open:home:0"


def test_help_section_buttons_retain_counts_and_arrow_navigation(sections):
    _text, markup = rich_ui.help_view("0", 0)
    buttons = _buttons_by_text(markup)
    expected = {
        "Session · 7": b"hm:help:0:0",
        "Info · 1": b"hm:help:1:0",
        "Threads · 1": b"hm:help:2:0",
        "Skills · 1": b"hm:help:skills:0",
    }
    assert {label: buttons[label].type.data for label in expected} == expected
    assert buttons["Next 2/2 · 7 total ›"].type.data == b"hm:help:0:1"

    page_text, page_markup = rich_ui.help_view("0", 1)
    assert page_text.rstrip().endswith("</p>")
    assert "items on this page" not in page_text.lower()
    assert "‹ 1/2 · 7 total" in _buttons_by_text(page_markup)


@pytest.mark.parametrize("view,page", [("home", 0), ("0", 0), ("0", 1), ("skills", 0)])
def test_help_category_buttons_have_no_ordinals_and_preserve_names_counts_and_order(monkeypatch, view, page):
    names = ["Session", "Info", "Configuration", "Tools & Skills", "Search", "Extra", "Threads", "Skills"]
    catalog = [(name, [(f"/cmd{i}", "Description") for i in range(34 + index)])
               for index, name in enumerate(names)]
    monkeypatch.setattr(rich_ui, "help_sections", lambda **_: catalog)

    _text, markup = rich_ui.help_view(view, page)
    categories = [button for row in markup.rows for button in row.buttons][:len(catalog)]
    assert [button.text for button in categories] == [
        f"{name} · {len(entries)}" for name, entries in catalog
    ]
    assert not any(re.match(r"^\d+\.\s", button.text) for button in categories)
    assert categories[0].text == "Session · 34"
    assert [button.type.data for button in categories] == [
        f"hm:help:{'skills' if name == 'Skills' else index}:0".encode()
        for index, (name, _entries) in enumerate(catalog)
    ]


def test_help_empty_sections_keep_final_footer(monkeypatch):
    monkeypatch.setattr(rich_ui, "help_sections", lambda **_: [])
    for view in ("home", "0", "skills"):
        text, _ = rich_ui.help_view(view)
        assert list(html_body(text))[-1].find("i").text == "Page 1 of 1 • total 0"
    assert "No commands are available" in rich_ui.help_view("skills")[0]


def settings_ui(tmp_path):
    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _: tmp_path)
    adapter._help_skill_commands = lambda _: {}
    return adapter, TelegramPersonalSettingsUI(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [("home", 0), ("0", 1), ("skills", 0)])
async def test_settings_back_returns_to_exact_help_page_in_place(tmp_path, sections, origin):
    adapter, ui = settings_ui(tmp_path)
    source = Source()
    await ui.open(source, message_id=55, help_origin=origin)
    state = next(iter(ui._states.values()))
    request = adapter.edits[-1]
    assert request.id == 55
    # The settings card is native Rich HTML: one <tr> per data row plus the header row.
    table_rows = request.rich_message.html.count("<tr>") - 1
    assert request.rich_message.html.endswith(f"<p><i>Page 1 of 1 • total {table_rows}</i></p>")
    old_id = state["id"]
    back = _buttons_by_text(request.reply_markup)["Back to help"].type.data.decode()
    # Normal setting changes retain the origin while rotating callback tokens.
    await ui.handle_callback(CallbackEvent(55), source, f"ps:{old_id}:memory-toggle")
    assert state["help_origin"] == origin
    assert "Back to help" in _buttons_by_text(adapter.edits[-1].reply_markup)
    await ui._render_home(state, note="Saved safely.")
    assert adapter.edits[-1].rich_message.html.endswith(f"<p><i>Page 1 of 1 • total {table_rows + 1}</i></p>")
    assert "Saved safely." in adapter.edits[-1].rich_message.html
    # Stale callback tokens cannot escape the private settings card.
    count = len(adapter.edits)
    await ui.handle_callback(CallbackEvent(55), source, back)
    assert len(adapter.edits) == count
    current_back = _buttons_by_text(adapter.edits[-1].reply_markup)["Back to help"].type.data.decode()
    await ui.handle_callback(CallbackEvent(55), source, current_back)
    assert adapter.edits[-1].id == 55
    assert adapter.edits[-1].rich_message.html.replace("\n", "") == rich_ui.help_view(*origin)[0].replace("\n", "")
    assert not ui._states
    assert not ui._pending
    assert not ui._owner_state


@pytest.mark.asyncio
@pytest.mark.parametrize("other,event_id", [
    (Source(user_id="999"), 55),
    (Source(chat_id="999"), 55),
    (Source(thread_id="9"), 55),
    (Source(), 56),
    (Source(chat_type="group"), 55),
])
async def test_settings_help_back_cannot_be_used_by_other_actor_or_message(tmp_path, sections, other, event_id):
    adapter, ui = settings_ui(tmp_path)
    await ui.open(Source(), message_id=55, help_origin=("0", 1))
    back = _buttons_by_text(adapter.edits[-1].reply_markup)["Back to help"].type.data.decode()
    count = len(adapter.edits)
    event = CallbackEvent(event_id)
    await ui.handle_callback(event, other, back)
    assert len(adapter.edits) == count
    assert event.answers[-1]


@pytest.mark.asyncio
async def test_direct_settings_open_clears_help_origin(tmp_path, sections):
    adapter, ui = settings_ui(tmp_path)
    await ui.open(Source(), message_id=55, help_origin=("0", 1))
    await ui.open(Source())
    assert "Back to help" not in _buttons_by_text(adapter.edits[-1].reply_markup)
    assert next(iter(ui._states.values()))["help_origin"] is None


@pytest.mark.asyncio
async def test_adapter_passes_help_origin_and_preserves_existing_dm_auth(monkeypatch, tmp_path):
    from gateway import mongo_multiuser
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    monkeypatch.setattr(mongo_multiuser, "enabled", lambda _: False)
    adapter = TelethonAdapter(PlatformConfig())
    calls = []
    cancelled = []

    async def open_settings(source, **kwargs):
        calls.append((source, kwargs))

    adapter._settings_ui = lambda: SimpleNamespace(open=open_settings)
    adapter._skills_ui = lambda: SimpleNamespace(cancel_for_source=cancelled.append)
    adapter._authorized = lambda _: True
    source = Source()

    async def get_source(_):
        return source

    adapter._source = get_source
    event = CallbackEvent(55)
    event.data = b"hm:personal:open:0:1"
    await adapter._on_callback_query(event)
    assert calls == [(source, {"message_id": 55, "help_origin": ("0", 1)})]
    assert cancelled == [source]
    # Original non-help button remains compatible and receives no help origin.
    event.data = b"hm:personal:open"
    await adapter._on_callback_query(event)
    assert calls[-1][1] == {"message_id": 55}
    count = len(calls)
    source = replace(source, chat_type="group")
    async def answer(*args, **kwargs):
        pass
    event.answer = answer
    event.data = b"hm:personal:open:home:0"
    await adapter._on_callback_query(event)
    assert len(calls) == count
    source = Source()
    adapter._authorized = lambda _: False
    await adapter._on_callback_query(event)
    assert len(calls) == count
