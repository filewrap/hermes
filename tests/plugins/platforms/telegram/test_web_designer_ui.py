"""Behaviour contracts for the web-designer Telegram UI.

Builders are pure: they take a run record / verdict and return
``(html, markup)``. The callback handler is exercised with a fake
adapter/event/source against the real run store in a temp home, and a
fake checkpoints module injected into ``sys.modules``.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from telethon import functions as tl_functions

from gateway.web_designer.store import create_run, load_run, save_run
from gateway.web_designer.types import Finding, RunRecord, Verdict
from plugins.platforms.telegram import web_designer_ui as ui

RUN_ID = "0123456789abcdef"
OWNER_ID = "111"
OTHER_ID = "999"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp profile home, installed as the routing process home."""
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: launch)
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: launch)
    return launch


def _run(home, **overrides):
    record = create_run(
        home=home, thread_id="t-1", user_id=OWNER_ID, chat_id="222",
        run_id=RUN_ID)
    for key, value in overrides.items():
        setattr(record, key, value)
    if overrides:
        save_run(record)
    return record


class _BlockImport:
    """A sys.meta_path finder that makes one module unimportable.

    Deleting a name from sys.modules cannot simulate a missing module
    once its file exists on disk — the next import reloads it. This
    blocks the import itself, so the fail-closed path is exercised
    for real.
    """

    def __init__(self, name):
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name:
            raise ImportError(f"{self.name} is not available yet")


def _block(monkeypatch, name):
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockImport(name), *sys.meta_path])


def _verdict(**overrides):
    data = {
        "judger": "visual", "passed": False,
        "findings": [Finding(
            rule_id="R012", judger="visual", severity="defect",
            location="hero heading", expected="type scale step 1.25",
            actual="arbitrary 23px size", evidence="tokens.css:4")],
        "unchecked": ["R002", "R008"],
    }
    data.update(overrides)
    return Verdict(**data)


def _button_data(button):
    """Callback payload of a Telethon inline button (bytes on its type)."""
    data = getattr(getattr(button, "type", None), "data", b"")
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def _buttons(markup):
    """(label, data, style-name) triples of every button in a markup."""
    if markup is None:
        return []
    out = []
    for row in markup.rows:
        for button in row.buttons:
            style = getattr(button, "style", None)
            name = None
            if style is not None:
                if getattr(style, "bg_success", None):
                    name = "success"
                elif getattr(style, "bg_danger", None):
                    name = "danger"
                elif getattr(style, "bg_primary", None):
                    name = "primary"
            out.append((button.text, _button_data(button), name))
    return out


class FakeEvent:
    def __init__(self):
        self.message_id = 7
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeAdapter:
    def __init__(self, *, authorized=True):
        self.gateway_runner = None
        self._authorized_value = authorized
        self.requests = []

    def _authorized(self, source):
        return self._authorized_value

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)


def _source(user_id=OWNER_ID):
    return SimpleNamespace(user_id=user_id, chat_id="222", thread_id=None)


def _run_handler(adapter, event, source, data):
    return asyncio.run(
        ui.handle_web_designer_callback(adapter, event, source, data))


# --- progress card -----------------------------------------------------------


def test_progress_card_shows_stage_budgets_and_badge():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp", stage="build",
                       input_tokens=1000, output_tokens=500,
                       cost_usd=0.5, tool_calls=3, iterations=2)
    html, markup = ui.progress_card(record)
    assert "Stage 17 / 30" in html
    assert "1,500 / 150,000" in html
    assert "$0.50 / $2.00" in html
    assert "Tool calls: 3 / 30" in html
    assert "Iterations: 2 / 20" in html
    assert "wd:badge:new" not in html
    assert 'data="badge:new"' in html
    assert "new function — enjoy" not in html  # the toast is the tap answer, not card text
    assert markup is None


def test_progress_card_verdicts_summary_and_empty_state():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp")
    html, _ = ui.progress_card(record)
    assert "Verdicts: none yet." in html
    record.stage_data = {"verdicts": [
        {"judger": "visual", "passed": True, "findings": [], "unchecked": []},
        {"judger": "a11y", "passed": False,
         "findings": [{"rule_id": "R001"}], "unchecked": ["R050"]},
    ]}
    html, _ = ui.progress_card(record)
    assert "visual — pass (no findings)" in html
    assert "a11y — fail (1 finding(s), unchecked: R050)" in html


# --- brief / direction / decision cards --------------------------------------


def test_brief_card_restates_fields_and_three_decisions():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp",
                       brief={"problem": "Sell jam", "audience": "Shoppers",
                              "primary_action": "Buy a jar",
                              "success_measure": "Orders",
                              "tone": "Warm", "non_goals": ["Blog"]})
    html, markup = ui.brief_card(record)
    assert "Sell jam" in html and "Shoppers" in html and "Buy a jar" in html
    buttons = _buttons(markup)
    assert ("Approve", f"wd:approve:{RUN_ID}", "success") in buttons
    assert ("Approve with changes", f"wd:changes:{RUN_ID}", "primary") in buttons
    assert ("Reject", f"wd:reject:{RUN_ID}", "danger") in buttons


def test_direction_card_picks_each_direction():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp")
    html, markup = ui.direction_card(record, [
        {"name": "Warm market", "description": "Cream and amber"},
        {"name": "Clean shelf", "description": "White and green"},
    ])
    assert "Warm market" in html and "Clean shelf" in html
    buttons = _buttons(markup)
    assert ("Pick Warm market", f"wd:direction:{RUN_ID}:0", "primary") in buttons
    assert ("Pick Clean shelf", f"wd:direction:{RUN_ID}:1", "primary") in buttons


def test_decision_card_selected_first_with_clear_state():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp")
    html, markup = ui.decision_card(record, "font", [
        {"label": "Inter", "selected": False},
        {"label": "Fraunces", "selected": True},
    ])
    buttons = _buttons(markup)
    assert buttons[0][0] == "Selected — Fraunces"
    assert buttons[0][2] == "success"
    assert "Selected: " in html
    assert all(f"wd:pick:{RUN_ID}:font:" in data for _, data, _ in buttons)


# --- preview / verdict / delivery cards --------------------------------------


def test_preview_card_says_so_plainly_without_a_preview():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp")
    html, markup = ui.preview_card(record, {})
    assert "No preview available." in html
    assert "1440px: no screenshot yet." in html
    assert "375px: no screenshot yet." in html
    assert ("Approve", f"wd:approve:{RUN_ID}", "success") in _buttons(markup)


def test_preview_card_lists_screenshots_and_preview_link():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp")
    html, _ = ui.preview_card(record, {
        "1440": "shots/1440.png", "768": "shots/768.png",
        "375": "shots/375.png",
        "preview_url": "https://example.invalid/preview",
    })
    assert "1440px: shots/1440.png" in html
    assert "375px: shots/375.png" in html
    assert "Open preview" in html
    assert "https://example.invalid/preview" in html


def test_verdict_card_finding_lines_unchecked_and_unjudged():
    html, markup = ui.verdict_card(_verdict())
    assert markup is None
    assert "R012 · defect · hero heading" in html
    assert "expected: type scale step 1.25; actual: arbitrary 23px size" in html
    assert "evidence: tokens.css:4" in html
    assert "Not checked: R002, R008" in html
    html, _ = ui.verdict_card(Verdict(judger="a11y", passed=False, unjudged=True))
    assert "Not judged: this judger could not run" in html
    html, _ = ui.verdict_card(Verdict(judger="visual", passed=True))
    assert "Not checked: none." in html


def test_delivery_card_has_no_deploy_or_publish():
    record = RunRecord(run_id=RUN_ID, thread_id="t-1", user_id=OWNER_ID,
                       chat_id="222", home="/tmp")
    html, markup = ui.delivery_card(record, {
        "contents": ["index.html", "tokens.json"],
        "judger_summary": {"visual": "pass", "a11y": "pass"},
        "limitations": ["No dark mode yet"],
        "revision_instructions": "Reply with consolidated feedback.",
    })
    assert "index.html" in html and "No dark mode yet" in html
    assert "Reply with consolidated feedback." in html
    assert "No deploy target." in html
    assert markup is None
    assert "Publish" not in html


# --- callback handler --------------------------------------------------------


def test_handler_ignores_non_wd_data(home):
    event = FakeEvent()
    assert _run_handler(FakeAdapter(), event, _source(), "ms:1:pick") is False
    assert event.answers == []


def test_handler_badge_tap_answers_the_toast(home):
    event = FakeEvent()
    assert _run_handler(FakeAdapter(), event, _source(), "wd:badge:new") is True
    assert event.answers == [("new function — enjoy", {})]


def test_handler_denies_a_stranger_silently(home):
    _run(home, status="awaiting_checkpoint")
    event = FakeEvent()
    calls = []
    fake_checkpoints = types.ModuleType("gateway.web_designer.checkpoints")
    fake_checkpoints.record_decision = lambda **kw: calls.append(kw)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "gateway.web_designer.checkpoints", fake_checkpoints)
        assert _run_handler(
            FakeAdapter(authorized=False), event, _source(OTHER_ID),
            f"wd:approve:{RUN_ID}") is True
    assert event.answers == [(None, {})]
    assert calls == []


def test_handler_approve_records_and_edits_card_in_place(home):
    _run(home, status="awaiting_checkpoint")
    event = FakeEvent()
    adapter = FakeAdapter()
    calls = []

    def record_decision(record, gate, decision, **kw):
        calls.append({"gate": gate, "decision": decision,
                      "run_id": record.run_id})

    fake = types.ModuleType("gateway.web_designer.checkpoints")
    fake.record_decision = record_decision
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "gateway.web_designer.checkpoints", fake)
        assert _run_handler(adapter, event, _source(), f"wd:approve:{RUN_ID}") is True
    assert calls == [{"gate": "brief", "decision": "approve", "run_id": RUN_ID}]
    assert event.answers == [(None, {})]
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert isinstance(request, tl_functions.messages.EditMessageRequest)
    assert "Brief approved." in request.message


def test_handler_direction_pick_records_choice(home):
    _run(home, status="awaiting_checkpoint",
         stage_data={"direction": {"directions": [
             {"name": "Warm market"}, {"name": "Clean shelf"}]}})
    event = FakeEvent()
    adapter = FakeAdapter()
    calls = []

    def record_decision(record, gate, decision, **kw):
        calls.append({"gate": gate, "decision": decision, "run_id": record.run_id})

    fake = types.ModuleType("gateway.web_designer.checkpoints")
    fake.record_decision = record_decision
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "gateway.web_designer.checkpoints", fake)
        assert _run_handler(
            adapter, event, _source(), f"wd:direction:{RUN_ID}:1") is True
    # The choice is persisted as stage data first, then the direction
    # gate is approved through checkpoints (no "pick" decision exists).
    assert load_run(home, RUN_ID).stage_data["direction"]["chosen"] == "Clean shelf"
    assert calls == [{"gate": "direction", "decision": "approve",
                      "run_id": RUN_ID}]
    assert event.answers == [(None, {})]
    assert len(adapter.requests) == 1
    assert "Direction Clean shelf picked." in adapter.requests[0].message


def test_handler_option_pick_persists_choice_without_a_gate(home):
    _run(home, stage_data={"font": {"families": ["Inter", "Fraunces"]}})
    event = FakeEvent()
    adapter = FakeAdapter()
    calls = []

    def record_decision(record, gate, decision, **kw):
        calls.append({"gate": gate, "decision": decision})

    fake = types.ModuleType("gateway.web_designer.checkpoints")
    fake.record_decision = record_decision
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "gateway.web_designer.checkpoints", fake)
        assert _run_handler(
            adapter, event, _source(), f"wd:pick:{RUN_ID}:font:1") is True
    # Font is not a checkpoint gate: the choice is stage data only.
    assert calls == []
    saved = load_run(home, RUN_ID).stage_data["font"]
    assert saved["chosen"] == "Fraunces" and saved["option_index"] == 1
    assert "font option Fraunces picked." in adapter.requests[0].message


def test_handler_changes_tap_asks_for_the_changes(home):
    _run(home, status="awaiting_checkpoint")
    event = FakeEvent()
    adapter = FakeAdapter()

    def record_decision(record, gate, decision, **kw):
        if decision == "approve_with_changes" and not kw.get("changes"):
            raise ValueError("approve_with_changes needs the listed changes")

    fake = types.ModuleType("gateway.web_designer.checkpoints")
    fake.record_decision = record_decision
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "gateway.web_designer.checkpoints", fake)
        assert _run_handler(
            adapter, event, _source(), f"wd:changes:{RUN_ID}") is True
    assert "Send the changes as a message" in adapter.requests[0].message


def test_handler_unknown_run_answers_silently(home):
    event = FakeEvent()
    assert _run_handler(
        FakeAdapter(), event, _source(), f"wd:approve:{RUN_ID}") is True
    assert event.answers == [(None, {})]


def test_handler_without_checkpoints_says_so_on_the_card(home, monkeypatch):
    _run(home, status="awaiting_checkpoint")
    event = FakeEvent()
    adapter = FakeAdapter()
    _block(monkeypatch, "gateway.web_designer.checkpoints")
    assert _run_handler(adapter, event, _source(), f"wd:approve:{RUN_ID}") is True
    assert event.answers == [(None, {})]
    assert len(adapter.requests) == 1
    assert "not available yet" in adapter.requests[0].message
