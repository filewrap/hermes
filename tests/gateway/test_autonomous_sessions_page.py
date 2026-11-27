"""Behaviour contracts for the autonomous sessions page.

The Sessions button edits the same message to the full session list:
one ``<details>`` collapsible per run, newest first, 10 per page. Each
run is identified by its goal, carries a table (turns, tokens, last
tool, recent tools, agents, model, times), an overview line the Copy
summary button copies, and a Resume button — only while the mode is
off, because one ongoing run stays primary. The current run carries a
small inline Current/Running badge: a tappable ``badge:<type>`` button
that answers its toast automatically. Tokens read compact, without brackets.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import autonomous_state
from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

OWNER_ID = "8495104954"
SESSION_KEY = "agent:main:telegram:dm:8495104954"


class _FakeAdapter:
    """Owner-checking adapter with card send, edit, pin, delete."""

    def __init__(self, *, card_message_id="777"):
        self.cards = []
        self.edits = []
        self.pins = []
        self.deletes = []
        self.card_message_id = card_message_id

    def _is_owner_user(self, user_id, source=None):
        return str(user_id) == OWNER_ID

    def _peer_for(self, chat_id):
        return ("peer", str(chat_id))

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        return True

    async def send_command_card(self, *, source, html, links=(), copies=(),
                                buttons=(), reply_to=None):
        self.cards.append({"html": html, "buttons": buttons})
        return self.card_message_id

    async def pin_chat_message(self, chat_id, message_id, *, unpin=False):
        self.pins.append((str(chat_id), str(message_id), unpin))
        return {"message_id": str(message_id), "pinned": not unpin}

    async def delete_message(self, chat_id, message_id):
        self.deletes.append((str(chat_id), str(message_id)))
        return True


def _callback_event(data, *, message_id=555):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    event.message_id = message_id
    source = SessionSource(
        platform=Platform.TELEGRAM, user_id=OWNER_ID, chat_id=OWNER_ID,
        user_name="owner", chat_type="dm")
    return event, source, answered


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def _run_with_burn(home, goal, *, steps, tokens, state="stopped",
                   recent_tools=("shell", "read"), agents=("model-a",)):
    record = autonomous_state.create_run(home, SESSION_KEY, goal)
    autonomous_state.start_run(home, record["run_id"])
    autonomous_state.checkpoint(
        home, record["run_id"], steps=steps,
        input_tokens=tokens // 2, output_tokens=tokens - tokens // 2,
        last_tool=recent_tools[-1], recent_tools=list(recent_tools),
        recent_agents=list(agents))
    if state == "stopped":
        autonomous_state.mark_stopped(home, record["run_id"])
        autonomous_state.set_active(home, False)
    elif state == "done":
        autonomous_state.mark_done(home, record["run_id"])
        autonomous_state.set_active(home, False)
    return autonomous_state.load_run(home, record["run_id"])


@pytest.mark.asyncio
async def test_sessions_tap_edits_in_place_and_lists_runs_with_tables(auto_home):
    adapter = _FakeAdapter()
    first = _run_with_burn(auto_home, "First goal", steps=4, tokens=1000)
    second = _run_with_burn(auto_home, "Second goal", steps=25, tokens=2_500_000)

    event, source, answered = _callback_event("hm:autonomous:sessions")
    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:sessions")

    assert handled is True
    assert adapter.cards == []  # same message, edited in place
    assert len(adapter.edits) == 1
    html = adapter.edits[0].message
    assert "Autonomous sessions" in html
    assert "First goal" in html and "Second goal" in html
    # Newest first: the second run's collapsible precedes the first.
    assert html.index("Second goal") < html.index("First goal")
    # Tables carry turns, tokens, tools, agents.
    assert "<table>" in html
    assert "25" in html and "2.5M / 1.0M" in html
    assert "[2.5M" not in html and "[1.0K" not in html
    assert "1.0K / 1.0M" in html
    assert "shell" in html and "model-a" in html
    # Large sessions show the overview bold.
    assert "<b>Second goal" in html
    # Copy summary rides as a tiny copy button; Resume per run while off.
    assert 'type="copy_text"' in html
    assert f'data="hm:autonomous:resume:{first["run_id"]}"' in html
    assert f'data="hm:autonomous:resume:{second["run_id"]}"' in html
    assert ">Back to autonomous</tg-button>" in html


@pytest.mark.asyncio
async def test_sessions_page_when_on_has_no_resume_buttons(auto_home):
    adapter = _FakeAdapter()
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "Live goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.checkpoint(
        auto_home, record["run_id"], steps=3,
        input_tokens=500, output_tokens=500, last_tool="shell")

    event, source, answered = _callback_event("hm:autonomous:sessions")
    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:sessions")

    assert handled is True
    html = adapter.edits[0].message
    assert "A run is ongoing" in html
    assert "hm:autonomous:resume:" not in html
    # The running run carries the small inline Running badge.
    assert 'data="badge:running"' in html
    assert ">running<" in html


@pytest.mark.asyncio
async def test_sessions_page_marks_current_stopped_run(auto_home):
    adapter = _FakeAdapter()
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "Current goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.mark_stopped(auto_home, record["run_id"])
    # The flag stays on with a stopped current run: it is still the
    # current session, so it carries the Current badge (not Running).

    event, source, answered = _callback_event("hm:autonomous:sessions")
    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:sessions")

    assert handled is True
    html = adapter.edits[0].message
    assert 'data="badge:current"' in html
    assert ">current<" in html


@pytest.mark.asyncio
async def test_sessions_page_paginates_ten_per_page(auto_home):
    adapter = _FakeAdapter()
    for index in range(11):
        _run_with_burn(auto_home, f"Goal {index}", steps=1, tokens=100)

    event, source, answered = _callback_event("hm:autonomous:sessions")
    await handle_autonomous_callback(adapter, event, source,
                                     "hm:autonomous:sessions")
    html = adapter.edits[0].message
    assert html.count("<details>") == 10
    assert "Next ›" in html

    event, source, answered = _callback_event("hm:autonomous:sessions:2")
    await handle_autonomous_callback(adapter, event, source,
                                     "hm:autonomous:sessions:2")
    html = adapter.edits[1].message
    assert html.count("<details>") == 1
    assert "‹ Previous" in html
    assert "Page 2 of 2" in html


@pytest.mark.asyncio
async def test_sessions_back_button_returns_to_status_card(auto_home):
    adapter = _FakeAdapter()
    _run_with_burn(auto_home, "First goal", steps=4, tokens=1000)

    event, source, answered = _callback_event("hm:autonomous:status")
    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:status")

    assert handled is True
    assert adapter.cards == []
    assert len(adapter.edits) == 1
    html = adapter.edits[0].message
    assert "Autonomous mode" in html
    assert "First goal" in html
    assert ">Enable (starts fresh)</tg-button>" in html


@pytest.mark.asyncio
async def test_current_and_running_badges_answer_their_toasts():
    """A badge tap answers automatically — the badge is a status
    indicator, never a dead pill."""
    from plugins.platforms.telegram.callback_answer import answer_badge_callback

    event = SimpleNamespace(answer=AsyncMock())

    assert await answer_badge_callback(event, "badge:current") is True
    event.answer.assert_awaited_once_with(":) current session")

    event = SimpleNamespace(answer=AsyncMock())
    assert await answer_badge_callback(event, "badge:running") is True
    event.answer.assert_awaited_once_with(":) running now")


@pytest.mark.asyncio
async def test_sessions_page_renders_dict_model_as_plain_text(auto_home):
    """The run record's stamped model is a {"model", "provider"} dict; the
    Agents and Model rows must read plain text ("model · provider"), never
    a Python dict repr."""
    adapter = _FakeAdapter()
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "Dict goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.checkpoint(
        auto_home, record["run_id"], steps=2,
        input_tokens=100, output_tokens=100,
        model={"model": "tencent/hy4-flash:free", "provider": "nous"})
    autonomous_state.mark_stopped(auto_home, record["run_id"])
    autonomous_state.set_active(auto_home, False)

    event, source, answered = _callback_event("hm:autonomous:sessions")
    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:sessions")

    assert handled is True
    html = adapter.edits[0].message
    assert "tencent/hy4-flash:free · nous" in html
    assert "{'model'" not in html


@pytest.mark.asyncio
async def test_sessions_page_action_row_has_copy_resume_delete_together(auto_home):
    """One row, three buttons: Copy summary, Resume, Delete ride in the
    same <tg-button-row>, not stacked one below the other."""
    adapter = _FakeAdapter()
    run = _run_with_burn(auto_home, "Row goal", steps=4, tokens=1000)

    event, source, answered = _callback_event("hm:autonomous:sessions")
    await handle_autonomous_callback(adapter, event, source,
                                     "hm:autonomous:sessions")
    html = adapter.edits[0].message

    row = next(line for line in html.splitlines()
               if f'data="hm:autonomous:resume:{run["run_id"]}"' in line)
    assert 'type="copy_text"' in row
    assert f'data="hm:autonomous:delete:{run["run_id"]}"' in row
    assert ">Delete</tg-button>" in row


@pytest.mark.asyncio
async def test_sessions_delete_tap_removes_run_and_rerenders(auto_home):
    adapter = _FakeAdapter()
    doomed = _run_with_burn(auto_home, "Doomed goal", steps=4, tokens=1000)
    kept = _run_with_burn(auto_home, "Kept goal", steps=2, tokens=500)

    event, source, answered = _callback_event("hm:autonomous:sessions")
    await handle_autonomous_callback(adapter, event, source,
                                     "hm:autonomous:sessions")
    event, source, answered = _callback_event(
        f"hm:autonomous:delete:{doomed['run_id']}")
    handled = await handle_autonomous_callback(
        adapter, event, source, f"hm:autonomous:delete:{doomed['run_id']}")

    assert handled is True
    assert autonomous_state.load_run(auto_home, doomed["run_id"]) is None
    assert autonomous_state.load_run(auto_home, kept["run_id"]) is not None
    html = adapter.edits[-1].message
    assert "Doomed goal" not in html
    assert "Kept goal" in html
    assert answered and "deleted" in str(answered[0][0]).lower()


@pytest.mark.asyncio
async def test_sessions_page_failed_run_summary_carries_failed_badge(auto_home):
    """A failed run's state reads as a small inline badge in the summary
    line — the same badge:<type> pattern as the Current/Running badges —
    never plain text."""
    adapter = _FakeAdapter()
    record = autonomous_state.create_run(auto_home, SESSION_KEY, "Failed goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.checkpoint(
        auto_home, record["run_id"], steps=150, last_tool="autonomous_done")
    autonomous_state.mark_failed(auto_home, record["run_id"],
                                 last_error="gateway restarted mid-run")
    autonomous_state.set_active(auto_home, False)

    event, source, answered = _callback_event("hm:autonomous:sessions")
    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:sessions")

    assert handled is True
    html = adapter.edits[0].message
    assert 'data="badge:failed"' in html
    assert ">failed<" in html


@pytest.mark.asyncio
async def test_failed_badge_answers_its_toast():
    """A failed-badge tap answers automatically, like every other badge."""
    from plugins.platforms.telegram.callback_answer import answer_badge_callback

    event = SimpleNamespace(answer=AsyncMock())

    assert await answer_badge_callback(event, "badge:failed") is True
    event.answer.assert_awaited_once_with(":( failed")


def test_session_overview_fits_the_copy_button_limit(auto_home):
    """The overview the Copy summary button copies never exceeds the
    256-character copy-button limit."""
    from plugins.platforms.telegram.autonomous_callback import _session_overview

    record = _run_with_burn(
        auto_home, "x" * 500, steps=100, tokens=9_999_999,
        recent_tools=("shell",))

    overview = _session_overview(record)

    assert len(overview) <= 256
    assert "10.0M tokens" in overview
    assert "[10.0M" not in overview
    assert "100 turns" in overview

    # With a budget, the overview carries used / budget, still in budget.
    budgeted = _session_overview(record, budget_cap=1_000_000)
    assert len(budgeted) <= 256
    assert "10.0M / 1.0M tokens" in budgeted
