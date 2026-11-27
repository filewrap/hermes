"""Behaviour contracts: the autonomous limits menu.

A dedicated menu for modifying sub-agent and swarm limits without
editing config.yaml: the owner changes a value by tapping a preset, the
write lands in the profile config, and the next run enforces it — the
menu proves that by reading the value back through the same getters the
enforcement uses. Worker models build from the model the coordinator
actually runs (the most recent run's stamped model); a non-owner tap
changes nothing and answers its reason. The Limits tap edits the current
message in place, never sends a new one. Presets show the current value
first, checked and green, the rest uncolored — the selection is
unmistakable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway import autonomous_state
from gateway.config import Platform
from gateway.session import SessionSource
from plugins.platforms.telegram.autonomous_callback import handle_autonomous_callback

OWNER_ID = "8495104954"
OTHER_ID = "12345"
SESSION_KEY = "agent:main:telegram:dm:8495104954"


def _make_source(*, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm",
                 platform=Platform.TELEGRAM):
    return SessionSource(
        platform=platform, user_id=user_id, chat_id=chat_id,
        user_name="owner", chat_type=chat_type,
    )


class _FakeAdapter:
    """Owner-checking adapter with card send, edit, and re-dispatch."""

    def __init__(self):
        self.cards = []
        self.edits = []
        self.dispatched = []

    def _is_owner_user(self, user_id, source=None):
        return str(user_id) == OWNER_ID

    async def send_command_card(self, *, source, html, links=(), copies=(),
                                buttons=(), reply_to=None):
        self.cards.append({"html": html, "buttons": buttons})
        return "4242"

    def _peer_for(self, chat_id):
        return ("peer", str(chat_id))

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        return True

    async def handle_message(self, event):
        self.dispatched.append(event.text)


def _callback_event(data, *, user_id=OWNER_ID, chat_id=OWNER_ID, chat_type="dm",
                    message_id=99):
    answered = []

    class Event:
        async def answer(self, *args, **kwargs):
            answered.append((args, kwargs))

    event = Event()
    event.data = data
    event.message_id = message_id
    source = SimpleNamespace(
        user_id=user_id, chat_id=chat_id, chat_type=chat_type,
        platform=Platform.TELEGRAM)
    return event, source, answered


@pytest.fixture
def auto_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _read_config(home):
    from hermes_cli.config import read_user_config_raw

    return read_user_config_raw(home / "config.yaml")


def _seed_run(home, model):
    record = autonomous_state.create_run(home, SESSION_KEY, "goal", model=model)
    autonomous_state.start_run(home, record["run_id"])
    return record


@pytest.mark.asyncio
async def test_set_limit_writes_config_and_next_run_reads_it(auto_home):
    from tools.delegate_tool_config import _get_max_concurrent_children

    adapter = _FakeAdapter()
    event, source, answered = _callback_event("hm:autonomous:set:maxc:2")

    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:set:maxc:2")

    assert handled is True
    assert _read_config(auto_home)["delegation"]["max_concurrent_children"] == 2
    assert _get_max_concurrent_children() == 2
    assert adapter.edits, "the menu re-renders in place after a set"
    assert answered == []


@pytest.mark.asyncio
async def test_set_limit_invalid_value_rejected(auto_home):
    adapter = _FakeAdapter()
    event, source, answered = _callback_event("hm:autonomous:set:maxc:0")

    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:set:maxc:0")

    assert handled is True
    assert "delegation" not in _read_config(auto_home)
    assert adapter.edits == []
    assert answered and "at least 1" in str(answered[0][0])


@pytest.mark.asyncio
async def test_worker_models_add_current_clear_remove(auto_home):
    from tools.delegate_tool_config import _get_worker_models

    adapter = _FakeAdapter()
    _seed_run(auto_home, "astra-x")

    event, source, answered = _callback_event("hm:autonomous:models:add")
    assert await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:models:add") is True
    assert _get_worker_models() == ["astra-x"]

    # Adding the same model twice does not duplicate it.
    event, source, answered = _callback_event("hm:autonomous:models:add")
    assert await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:models:add") is True
    assert _get_worker_models() == ["astra-x"]

    # A second model joins from the next run's stamped model.
    _seed_run(auto_home, "fable-y")
    event, source, answered = _callback_event("hm:autonomous:models:add")
    assert await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:models:add") is True
    assert _get_worker_models() == ["astra-x", "fable-y"]

    event, source, answered = _callback_event("hm:autonomous:models:rm:0")
    assert await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:models:rm:0") is True
    assert _get_worker_models() == ["fable-y"]

    event, source, answered = _callback_event("hm:autonomous:models:clear")
    assert await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:models:clear") is True
    assert _get_worker_models() == []


@pytest.mark.asyncio
async def test_limits_menu_non_owner_is_denied_with_toast(auto_home):
    from agent.i18n import t

    adapter = _FakeAdapter()
    event, source, answered = _callback_event(
        "hm:autonomous:set:maxc:2", user_id=OTHER_ID, chat_id=OTHER_ID)

    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:set:maxc:2")

    assert handled is True
    assert answered == [(("Autonomous controls are for the owner, in your DM.",), {})]
    assert answered[0][0][0] == t("gateway.autonomous.not_owner")
    assert "delegation" not in _read_config(auto_home)
    assert adapter.cards == [] and adapter.edits == []


@pytest.mark.asyncio
async def test_limits_menu_opens_and_shows_current_values(auto_home):
    adapter = _FakeAdapter()
    event, source, answered = _callback_event("hm:autonomous:limits")

    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:limits")

    assert handled is True
    # The Limits tap edits the current message in place — no new card.
    assert adapter.cards == []
    assert len(adapter.edits) == 1
    html = adapter.edits[0].message
    assert "Max workers" in html and "4" in html
    assert "Run budget" in html
    assert "<table>" in html
    # Navigation rides in-body as rich buttons; no keyboard buttons.
    assert ">Sub-agent limits</tg-button>" in html
    assert ">Swarm limits</tg-button>" in html
    assert ">Back to autonomous</tg-button>" in html


@pytest.mark.asyncio
async def test_kill_button_redispatches_kill_command(auto_home):
    adapter = _FakeAdapter()
    event, source, answered = _callback_event("hm:autonomous:limits:kill")

    handled = await handle_autonomous_callback(adapter, event, source,
                                               "hm:autonomous:limits:kill")

    assert handled is True
    assert adapter.dispatched == ["/autonomous kill"]


@pytest.mark.asyncio
async def test_status_card_has_limits_button(auto_home):
    from plugins.platforms.telegram.autonomous_callback import send_status_card

    adapter = _FakeAdapter()
    source = _make_source()

    message_id = await send_status_card(adapter, source)

    assert message_id == "4242"
    assert adapter.cards[0]["buttons"] == ()
    assert ">Limits</tg-button>" in adapter.cards[0]["html"]


def test_limits_menu_titles_render_bold_not_literal_tags(auto_home):
    """Menu titles are headings, not literal <b> tags: the body is direct
    HTML (like the business cards), never lines_to_rich_html — which
    escapes raw tags into visible text."""
    from plugins.platforms.telegram.autonomous_callback import _limits_menu_body

    for page, title in (("main", "Autonomous limits"),
                        ("workers", "Sub-agent limits"),
                        ("swarm", "Swarm limits")):
        html = _limits_menu_body(auto_home, page)
        assert "&lt;b&gt;" not in html
        assert f"<b>{title}</b>" not in html
        assert f"<h1>{title}</h1>" in html


def test_coordinator_model_is_plain_id_for_dict_shaped_record(auto_home):
    """The run record's stamped model is a dict {"model", "provider"} (see
    turn_model_from_agent); the menu must show the plain model id, never a
    Python repr — in the reader and in the "Add current model" button."""
    from plugins.platforms.telegram.autonomous_callback import (
        _current_coordinator_model, _limits_menu_body)

    _seed_run(auto_home, {"model": "qwen/qwen4-max:free", "provider": "nous"})

    assert _current_coordinator_model(auto_home) == "qwen/qwen4-max:free"

    html = _limits_menu_body(auto_home, "workers")
    assert ">Add current model (qwen/qwen4-max:free)</tg-button>" in html
    assert "{'model'" not in html


def test_limits_menu_preset_rows_mark_current_value_selected(auto_home):
    """Redesign contract: presets ride in-body as rich buttons, the current
    value comes first, checked (success) style, the rest are uncolored link
    style, and one italic line explains the marking. No keyboard buttons
    anywhere. Values show in block codes, in a table."""
    from plugins.platforms.telegram.autonomous_callback import _limits_menu_body

    html = _limits_menu_body(auto_home, "swarm")
    assert "<tg-button-row>" in html
    assert "✓ Green is the current value" in html
    assert "<table>" in html
    # Max workers defaults to 4: its button is checked and selected, 2 is not.
    assert ('type="callback_data" style="success" data="hm:autonomous:set:maxc:4">✓ 4</tg-button>'
            in html)
    assert ('type="callback_data" style="link" data="hm:autonomous:set:maxc:2">2</tg-button>'
            in html)
    # Selected first: the checked button precedes the unselected ones.
    assert html.index(">✓ 4</tg-button>") < html.index(">2</tg-button>")
    # Current values show in block codes beside their labels.
    assert "Max workers: <code>4</code>" in html


def test_workeriter_presets_include_1000_2000_and_unlimited(auto_home):
    """Worker iterations is a settable limit, not a silent fixed number:
    the menu offers 1000 and 2000, and an unlimited preset that stores 0 —
    0 reads as unset, so no cap applies in autonomous mode."""
    from plugins.platforms.telegram.autonomous_callback import _limits_menu_body

    html = _limits_menu_body(auto_home, "workers")

    assert 'data="hm:autonomous:set:workeriter:1000"' in html
    assert 'data="hm:autonomous:set:workeriter:2000"' in html
    assert 'data="hm:autonomous:set:workeriter:0">✓ unlimited</tg-button>' in html
    assert "Worker iterations: <code>unlimited</code>" in html


@pytest.mark.asyncio
async def test_setting_workeriter_to_unlimited_writes_zero_and_reads_unset(auto_home):
    """Tapping unlimited stores 0 — the config writer refuses to omit a
    key — and the readers treat 0 as unset, so the next run reads it
    back as no cap."""
    from tools.delegate_tool_config import _get_max_iterations_per_worker

    adapter = _FakeAdapter()
    event, source, answered = _callback_event("hm:autonomous:set:workeriter:100")
    await handle_autonomous_callback(adapter, event, source,
                                     "hm:autonomous:set:workeriter:100")
    assert _get_max_iterations_per_worker() == 100

    event, source, answered = _callback_event("hm:autonomous:set:workeriter:0")
    handled = await handle_autonomous_callback(
        adapter, event, source, "hm:autonomous:set:workeriter:0")

    assert handled is True
    assert _read_config(auto_home)["delegation"]["max_iterations_per_worker"] == 0
    assert _get_max_iterations_per_worker() is None


def test_limits_pages_carry_one_hint_per_setting(auto_home):
    """Each setting on the workers and swarm pages gets one short hint
    line, next to the setting it explains — plain words, no paragraph."""
    from plugins.platforms.telegram.autonomous_callback import _limits_menu_body

    workers = _limits_menu_body(auto_home, "workers")
    assert "off means no cap" in workers  # worker budget off
    assert "no cap in autonomous mode" in workers  # worker iterations unset
    assert "next worker model" in workers  # model fallback on

    swarm = _limits_menu_body(auto_home, "swarm")
    assert "run at the same time" in swarm  # max workers
    assert "share one model" in swarm  # per-model cap
    assert "between starting each worker" in swarm  # spawn stagger
    assert "Total tokens for the whole run" in swarm  # run budget
    assert "retries a rate-limited call" in swarm  # retries
