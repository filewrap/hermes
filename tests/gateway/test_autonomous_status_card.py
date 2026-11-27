"""Behaviour contracts for the bare-/autonomous status card and the Enable tap.

Bare ``/autonomous`` sends one rich card instead of plain text: a status
badge (enabled/disabled, ``badge:<type>`` toast), what the mode is and
how it works, the last sessions with per-run token burn and a total
(tokens compact, without brackets), and rich in-body buttons by state —
disabled: the big Enable button (worded so it starts fresh), Sessions,
Limits, Resume Previous; enabled: Disable and Sessions only, no Resume
and no Limits, because limits cannot change mid-run and one ongoing run
stays primary. The Enable tap (``hm:autonomous:enable``) starts a fresh
run directly; Resume Previous reuses the existing resume flow, on any
run — a done run re-opens too. Disable unpins and edits the same message
in place to the disabled card, never deletes it. Gating stays
owner-only + DM-only + Telegram-only; a denied tap answers its reason
in a toast, never silently.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway import autonomous_state
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

OWNER_ID = "8495104954"
OTHER_ID = "12345"


def _make_event(text, *, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm",
                platform=Platform.TELEGRAM):
    source = SessionSource(
        platform=platform, user_id=user_id, chat_id=chat_id,
        user_name="owner", chat_type=chat_type,
    )
    return MessageEvent(text=text, source=source)


class _FakeAdapter:
    """Owner-checking adapter with a card method; records pins/deletes/edits."""

    def __init__(self, *, card_message_id="777"):
        self.cards = []
        self.pins = []
        self.deletes = []
        self.edits = []
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


def _make_runner(adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._delivery_adapter_for = lambda source: adapter
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:8495104954"
    runner._normalize_source_for_session_key = lambda source: source
    runner._reply_anchor_for_event = lambda event: None
    return runner


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def _master_mode(home):
    from hermes_cli.config import read_user_config_raw

    config = read_user_config_raw(home / "config.yaml")
    return bool((config.get("platforms") or {}).get("telegram", {}).get("autonomous_mode", False))


def _run_with_burn(home, goal, *, steps, tokens, state="stopped"):
    """A finished run record with ledger numbers (checkpointed, then stopped)."""
    record = autonomous_state.create_run(home, "agent:main:telegram:dm:8495104954", goal)
    autonomous_state.start_run(home, record["run_id"])
    autonomous_state.checkpoint(
        home, record["run_id"], steps=steps,
        input_tokens=tokens // 2, output_tokens=tokens - tokens // 2,
        last_tool="shell")
    if state == "stopped":
        autonomous_state.mark_stopped(home, record["run_id"])
        # mark_stopped moves only the record; the flag clears with the
        # disable flow, which the helper mirrors for a clean off state.
        autonomous_state.set_active(home, False)
    elif state == "done":
        autonomous_state.mark_done(home, record["run_id"])
        autonomous_state.set_active(home, False)
    return autonomous_state.load_run(home, record["run_id"])


@pytest.mark.asyncio
async def test_bare_off_card_has_disabled_badge_enable_button_no_sessions(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None  # card sent; the runner sends nothing else
    assert len(adapter.cards) == 1
    card = adapter.cards[0]
    assert "Autonomous mode" in card["html"]
    assert 'data="badge:disabled"' in card["html"]
    assert ">disabled<" in card["html"]
    # The green pill means the mode is enabled; off, the card carries
    # only the red disabled badge.
    assert '<tg-button type="disabled" style="success">autonomous</tg-button>' not in card["html"]
    assert "No sessions yet." in card["html"]
    assert "no approvals, no turn limits" in card["html"]
    assert "never turns itself off" in card["html"]
    assert card["buttons"] == ()
    assert ">Enable (starts fresh)</tg-button>" in card["html"]
    assert 'data="hm:autonomous:enable"' in card["html"]
    assert ">Sessions</tg-button>" in card["html"]
    assert 'data="hm:autonomous:sessions"' in card["html"]
    assert ">Limits</tg-button>" in card["html"]


@pytest.mark.asyncio
async def test_bare_off_card_with_sessions_lists_runs_tokens_and_resume(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    _run_with_burn(auto_home, "First goal", steps=4, tokens=1000)
    second = _run_with_burn(auto_home, "Second goal", steps=8, tokens=3000)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert "First goal" in card["html"]
    assert "Second goal" in card["html"]
    assert "1.0K / 1.0M tokens" in card["html"]
    assert "3.0K / 1.0M tokens" in card["html"]
    assert "Total burn: 4.0K tokens" in card["html"]
    assert card["buttons"] == ()
    assert ">Enable (starts fresh)</tg-button>" in card["html"]
    assert ">Resume previous</tg-button>" in card["html"]
    assert f'data="hm:autonomous:resume:{second["run_id"]}"' in card["html"]
    assert ">Limits</tg-button>" in card["html"]


@pytest.mark.asyncio
async def test_bare_off_card_shows_configured_budget_not_hardcoded_million(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    (auto_home / "config.yaml").write_text(
        "autonomous:\n  max_tokens_per_run: 250000\n", encoding="utf-8")
    _run_with_burn(auto_home, "Big goal", steps=8, tokens=450_000)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert "450.0K / 250.0K tokens" in card["html"]


@pytest.mark.asyncio
async def test_bare_off_card_shows_used_only_when_budget_disabled(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    (auto_home / "config.yaml").write_text(
        "autonomous:\n  max_tokens_per_run: 0\n", encoding="utf-8")
    _run_with_burn(auto_home, "Big goal", steps=8, tokens=450_000)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert "450.0K tokens" in card["html"]
    assert "450.0K /" not in card["html"]


@pytest.mark.asyncio
async def test_bare_off_card_marks_resumable_run_and_singular_counts(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    _run_with_burn(auto_home, "", steps=1, tokens=100)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert "(no goal)" in card["html"]
    assert "1 step" in card["html"]
    assert "1 steps" not in card["html"]
    assert "1 session" in card["html"]
    assert "1 sessions" not in card["html"]
    assert "resumable" in card["html"]
    assert "A goal is only recorded when you type it after /autonomous enable." in card["html"]


@pytest.mark.asyncio
async def test_bare_off_card_marks_only_the_resumable_run(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    _run_with_burn(auto_home, "First goal", steps=4, tokens=1000, state="done")
    second = _run_with_burn(auto_home, "Second goal", steps=8, tokens=3000)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert card["buttons"] == ()
    assert ">Enable (starts fresh)</tg-button>" in card["html"]
    assert f'data="hm:autonomous:resume:{second["run_id"]}"' in card["html"]
    assert ">Limits</tg-button>" in card["html"]
    # Newest first: the stopped run's line precedes the done run's line.
    second_line = card["html"].split("Second goal", 1)[1].split("First goal", 1)[0]
    first_line = card["html"].split("First goal", 1)[1]
    assert "resumable" in second_line
    assert "resumable" not in first_line


@pytest.mark.asyncio
async def test_bare_on_card_has_enabled_badge_disable_button_and_live_run(auto_home):
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    run_id = autonomous_state.load_state(auto_home)["current_run_id"]
    autonomous_state.checkpoint(
        home=auto_home, run_id=run_id, steps=12,
        input_tokens=3000, output_tokens=1203, last_tool="shell")
    adapter.cards.clear()

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert 'data="badge:enabled"' in card["html"]
    assert ">enabled<" in card["html"]
    # On, the green pill rides atop the card.
    assert '<tg-button type="disabled" style="success">autonomous</tg-button>' in card["html"]
    assert "running" in card["html"]
    assert "(no goal)" in card["html"]
    assert "12 steps" in card["html"]
    assert "4.2K / 1.0M tokens" in card["html"]
    assert "last: shell" in card["html"]
    assert card["buttons"] == ()
    assert ">Disable</tg-button>" in card["html"]
    assert 'data="hm:autonomous:disable"' in card["html"]
    # Enabled: Disable and Sessions only — no Resume, no Limits.
    assert ">Sessions</tg-button>" in card["html"]
    assert 'data="hm:autonomous:sessions"' in card["html"]
    assert ">Resume" not in card["html"]
    assert ">Limits</tg-button>" not in card["html"]


@pytest.mark.asyncio
async def test_bare_card_failure_falls_back_to_plain_reply(auto_home):
    adapter = _FakeAdapter(card_message_id=None)
    runner = _make_runner(adapter)

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is not None and "Autonomous mode is off" in result
    assert len(adapter.cards) == 1  # the card was attempted, then fell back


def _callback_event(data, *, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm",
                    message_id="555"):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    event.message_id = message_id
    source = SessionSource(
        platform=Platform.TELEGRAM, user_id=user_id, chat_id=chat_id,
        user_name="owner", chat_type=chat_type)
    return event, source, answered


@pytest.mark.asyncio
async def test_enable_callback_starts_fresh_run_pins_and_persists(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    event, source, answered = _callback_event("hm:autonomous:enable")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:enable")

    assert handled is True
    assert answered == [((), {})]
    assert autonomous_state.is_active(auto_home) is True
    assert _master_mode(auto_home) is True
    state = autonomous_state.load_state(auto_home)
    assert state["pinned_message_id"] == "777"
    assert state["chat_id"] == OWNER_ID
    record = autonomous_state.load_run(auto_home, state["current_run_id"])
    assert record is not None and record["state"] == "running"
    assert record["session_key"] == "agent:main:telegram:dm:8495104954"
    assert (OWNER_ID, "555") in adapter.deletes  # status card deleted
    assert len(adapter.cards) == 1  # the enable card
    assert adapter.cards[0]["buttons"] == ()
    assert ">Disable</tg-button>" in adapter.cards[0]["html"]
    assert (OWNER_ID, "777", False) in adapter.pins


@pytest.mark.asyncio
async def test_enable_callback_when_already_active_answers_and_starts_nothing(auto_home):
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    first_run = autonomous_state.load_state(auto_home)["current_run_id"]
    adapter.cards.clear()
    event, source, answered = _callback_event("hm:autonomous:enable")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:enable")

    assert handled is True
    assert answered == [(("Autonomous mode is already on.",), {})]
    assert autonomous_state.load_state(auto_home)["current_run_id"] == first_run
    assert adapter.cards == []


@pytest.mark.asyncio
async def test_enable_callback_non_owner_is_denied_with_toast(auto_home):
    from agent.i18n import t
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    event, source, answered = _callback_event(
        "hm:autonomous:enable", user_id=OTHER_ID, chat_id=OTHER_ID)

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:enable")

    assert handled is True
    assert answered == [(("Autonomous controls are for the owner, in your DM.",), {})]
    assert answered[0][0][0] == t("gateway.autonomous.not_owner")
    assert autonomous_state.is_active(auto_home) is False
    assert adapter.cards == [] and adapter.pins == []


@pytest.mark.asyncio
async def test_enable_callback_non_dm_is_denied_with_toast(auto_home):
    from agent.i18n import t
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    event, source, answered = _callback_event(
        "hm:autonomous:enable", chat_id="-1001", chat_type="group")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:enable")

    assert handled is True
    assert answered == [(("Autonomous controls are for the owner, in your DM.",), {})]
    assert answered[0][0][0] == t("gateway.autonomous.not_owner")
    assert autonomous_state.is_active(auto_home) is False
    assert adapter.cards == []


@pytest.mark.asyncio
async def test_on_callback_query_routes_autonomous_enable(auto_home, monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    monkeypatch.setattr(adapter, "_source", AsyncMock(return_value=SessionSource(
        platform=Platform.TELEGRAM, chat_id=OWNER_ID, user_id=OWNER_ID,
        user_name="owner", chat_type="dm")))
    monkeypatch.setattr(adapter, "_authorized", lambda _source: True)
    monkeypatch.setattr(
        adapter, "_is_owner_user", lambda user_id, source=None: str(user_id) == OWNER_ID)
    monkeypatch.setattr(adapter, "pin_chat_message", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "delete_message", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "send_command_card", AsyncMock(return_value="777"))
    event, _, answered = _callback_event("hm:autonomous:enable")

    await adapter._on_callback_query(event)

    assert answered == [((), {})]
    assert autonomous_state.is_active(auto_home) is True


def test_ago_text_ranges():
    from plugins.platforms.telegram.autonomous_callback import _ago_text
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert _ago_text(now.isoformat()) == "just now"
    assert _ago_text((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert _ago_text((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert _ago_text((now - timedelta(days=2)).isoformat()) == "2d ago"
    assert _ago_text("not a date") == ""


def test_recent_runs_returns_latest_first_limited(tmp_path):
    import json

    home = tmp_path / "home"
    made = []
    for goal in ("first", "second", "third", "fourth"):
        record = autonomous_state.create_run(home, "s", goal)
        made.append(record)
    # Distinct last_step_at values, out of creation order.
    from datetime import UTC, datetime, timedelta

    base = datetime.now(UTC)
    for record, hours in zip(made, (4, 1, 3, 2)):
        path = home / "autonomous" / "runs" / f"{record['run_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["last_step_at"] = (base - timedelta(hours=hours)).isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")

    runs = autonomous_state.recent_runs(home, limit=3)

    assert [run["goal"] for run in runs] == ["second", "fourth", "third"]
    assert autonomous_state.recent_runs(home, limit=1)[0]["goal"] == "second"
    assert autonomous_state.recent_runs(tmp_path / "missing") == []


@pytest.mark.asyncio
async def test_bare_on_card_with_failed_run_has_no_resume_or_limits(auto_home):
    """The mode stays on after a failed run (the flag is the owner's
    switch), but the enabled card carries no Resume and no Limits — the
    owner stops it first, then resumes from the disabled card or the
    sessions page. One ongoing run stays primary."""
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    record = autonomous_state.create_run(
        auto_home, "agent:main:telegram:dm:8495104954", "Failed goal")
    autonomous_state.start_run(auto_home, record["run_id"])
    autonomous_state.mark_failed(auto_home, record["run_id"], last_error="boom")

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    assert autonomous_state.is_active(auto_home) is True  # mode stays on
    card = adapter.cards[0]
    assert card["buttons"] == ()
    assert ">Disable</tg-button>" in card["html"]
    assert ">Sessions</tg-button>" in card["html"]
    assert ">Resume" not in card["html"]
    assert ">Limits</tg-button>" not in card["html"]
    assert "Failed goal" in card["html"]


@pytest.mark.asyncio
async def test_bare_off_card_resume_previous_points_at_done_run(auto_home):
    """Resume has no state limit except killed: when the newest resumable
    run is a finished one, Resume Previous re-opens it."""
    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    done = _run_with_burn(auto_home, "Done goal", steps=6, tokens=2000, state="done")

    result = await runner._handle_autonomous_command(_make_event("/autonomous"))

    assert result is None
    card = adapter.cards[0]
    assert ">Resume previous</tg-button>" in card["html"]
    assert f'data="hm:autonomous:resume:{done["run_id"]}"' in card["html"]


@pytest.mark.asyncio
async def test_disable_callback_unpins_and_edits_card_in_place(auto_home):
    """Disable unpins the enable card and edits the same message to the
    disabled status card — the message stays, the badge shows disabled,
    and nothing is deleted."""
    from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

    adapter = _FakeAdapter()
    runner = _make_runner(adapter)
    await runner._handle_autonomous_command(_make_event("/autonomous enable"))
    state = autonomous_state.load_state(auto_home)
    assert state["pinned_message_id"] == "777"
    event, source, answered = _callback_event("hm:autonomous:disable", message_id="777")

    handled = await handle_autonomous_callback(adapter, event, source, "hm:autonomous:disable")

    assert handled is True
    assert autonomous_state.is_active(auto_home) is False
    assert (OWNER_ID, "777", True) in adapter.pins  # unpinned
    assert adapter.deletes == []  # the card stays
    assert len(adapter.edits) == 1
    edited_html = adapter.edits[0].message
    assert 'data="badge:disabled"' in edited_html
    assert ">disabled<" in edited_html
    assert ">Enable (starts fresh)</tg-button>" in edited_html
    assert answered == [(("Autonomous mode disabled.",), {})]
