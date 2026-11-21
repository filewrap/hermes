"""Behaviour contracts for the ``web-designer`` tools.

Handlers are called directly against the real run store in a temp
launch home; the machine-worker modules (``load_gate``, ``checkpoints``,
``judgers``, ``machine``) are faked by injecting modules into
``sys.modules`` — the tools only call those modules' public functions.
Surface tests mirror the autonomous_done shape: own toolset, never
core, deferred by default, invisible to ordinary turns.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

import tools.web_designer_tool as tool
from gateway.web_designer.store import (
    create_run, list_runs, load_run, save_run, start_run,
)
from gateway.web_designer.types import Finding, Verdict
from hermes_cli.config_defaults import DEFAULT_CONFIG
from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset
from tools.registry import registry
from tools.tool_search import is_deferrable_tool_name

RUN_ID = "0123456789abcdef"


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    """A fake launch home, installed as the routing process home."""
    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(
        "hermes_constants.get_routing_process_hermes_home", lambda: home)
    return home


def _agent(user_id="111"):
    from types import SimpleNamespace

    return SimpleNamespace(
        gateway_session_key="agent:main:telegram:dm:222",
        session_id="sess-1", user_id=user_id, chat_id="222")


def _fake_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _install(monkeypatch, name, **attrs):
    module = _fake_module(name, **attrs)
    monkeypatch.setitem(sys.modules, name, module)
    return module


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


def _started_run(home):
    record = create_run(
        home=home, thread_id="agent:main:telegram:dm:222",
        user_id="111", chat_id="222", run_id=RUN_ID)
    start_run(record)
    return record


# --- start -------------------------------------------------------------------


def test_start_declines_with_the_gate_reason(launch_home, monkeypatch):
    _install(monkeypatch, "gateway.web_designer.load_gate",
             decide_load=lambda **kw: {
                 "load": False, "reason": "out_of_scope",
                 "redirect": "Ask for a mobile app instead.", "run": None})
    result = json.loads(tool.web_designer_start(
        "Build me a mobile app", parent_agent=_agent()))
    assert result["status"] == "declined"
    assert result["reason"] == "out_of_scope"
    assert result["redirect"] == "Ask for a mobile app instead."
    assert list_runs(launch_home) == []


def test_start_accepts_a_gate_run_id(launch_home, monkeypatch):
    record = create_run(
        home=launch_home, thread_id="agent:main:telegram:dm:222",
        user_id="111", chat_id="222", run_id=RUN_ID)
    _install(monkeypatch, "gateway.web_designer.load_gate",
             decide_load=lambda **kw: {
                 "load": True, "reason": "website", "redirect": None,
                 "run": record})
    result = json.loads(tool.web_designer_start(
        "Build a jam shop site", parent_agent=_agent()))
    assert result == {"status": "started", "run_id": RUN_ID}


def test_start_creates_the_run_when_the_gate_only_approves(
        launch_home, monkeypatch):
    _install(monkeypatch, "gateway.web_designer.load_gate",
             decide_load=lambda **kw: {
                 "load": True, "reason": "website", "redirect": None,
                 "run": None})
    result = json.loads(tool.web_designer_start(
        "Build a jam shop site", parent_agent=_agent()))
    assert result["status"] == "started"
    record = load_run(launch_home, result["run_id"])
    assert record is not None and record.status == "running"


def test_start_fails_closed_without_the_gate_module(launch_home, monkeypatch):
    _block(monkeypatch, "gateway.web_designer.load_gate")
    result = json.loads(tool.web_designer_start("Build a site"))
    assert "error" in result
    assert "not available yet" in result["error"]


def test_start_refuses_empty_request(launch_home):
    result = json.loads(tool.web_designer_start("   "))
    assert "error" in result


# --- status / approve / stop / judge -----------------------------------------


def test_status_reports_stage_budgets_and_verdicts(launch_home):
    record = _started_run(launch_home)
    record.input_tokens = 400
    record.output_tokens = 100
    record.cost_usd = 0.25
    record.tool_calls = 4
    record.stage_data = {"verdicts": [
        {"judger": "visual", "passed": True, "findings": [], "unchecked": []}]}
    save_run(record)
    result = json.loads(tool.web_designer_status(RUN_ID))
    assert result["status"] == "running"
    assert result["stage"] == "load_gate"
    assert result["tokens"]["total"] == 500
    assert result["cost_usd"] == 0.25
    assert result["tool_calls"] == 4
    assert result["verdicts"] == [
        {"judger": "visual", "passed": True, "findings": 0,
         "unchecked": [], "unjudged": False}]
    assert result["tools"]["total"] == 64
    assert result["tools"]["live"] == 9
    assert result["tools"]["reference"] == 55
    assert "WebAIM Contrast Checker" in result["tools"]["available_live"]


def test_status_of_a_missing_run_is_an_error(launch_home):
    result = json.loads(tool.web_designer_status("no-such-run"))
    assert "error" in result


def test_approve_records_through_checkpoints(launch_home, monkeypatch):
    _started_run(launch_home)
    calls = []

    def record_decision(record, gate, decision, **kw):
        calls.append({"gate": gate, "decision": decision,
                      "run_id": record.run_id, "changes": kw.get("changes")})
        record.checkpoint_decisions.append(
            {"gate": gate, "decision": decision})

    _install(monkeypatch, "gateway.web_designer.checkpoints",
             record_decision=record_decision)
    result = json.loads(tool.web_designer_approve(
        RUN_ID, "brief", "approve", parent_agent=_agent()))
    assert result["status"] == "recorded"
    assert result["gate"] == "brief" and result["decision"] == "approve"
    assert calls == [{"gate": "brief", "decision": "approve",
                      "run_id": RUN_ID, "changes": None}]


def test_approve_with_changes_carries_the_note(launch_home, monkeypatch):
    _started_run(launch_home)
    calls = []

    def record_decision(record, gate, decision, **kw):
        calls.append(kw.get("changes"))

    _install(monkeypatch, "gateway.web_designer.checkpoints",
             record_decision=record_decision)
    result = json.loads(tool.web_designer_approve(
        RUN_ID, "brief", "approve_with_changes",
        note="Warmer hero, less copy", parent_agent=_agent()))
    assert result["status"] == "recorded"
    assert calls == ["Warmer hero, less copy"]
    assert "error" in json.loads(tool.web_designer_approve(
        RUN_ID, "brief", "approve_with_changes", parent_agent=_agent()))


def test_approve_pick_persists_the_direction(launch_home, monkeypatch):
    record = _started_run(launch_home)
    record.stage_data = {"direction": {"directions": [
        {"name": "Warm market"}, {"name": "Clean shelf"}]}}
    save_run(record)
    calls = []

    def record_decision(record, gate, decision, **kw):
        calls.append({"gate": gate, "decision": decision})

    _install(monkeypatch, "gateway.web_designer.checkpoints",
             record_decision=record_decision)
    result = json.loads(tool.web_designer_approve(
        RUN_ID, "direction", "pick", choice=1, parent_agent=_agent()))
    assert result["status"] == "recorded"
    assert result["decision"] == "pick" and result["chosen"] == "Clean shelf"
    assert calls == [{"gate": "direction", "decision": "approve"}]
    assert load_run(launch_home, RUN_ID).stage_data["direction"]["chosen"] == "Clean shelf"
    assert "error" in json.loads(tool.web_designer_approve(
        RUN_ID, "brief", "pick", choice=0, parent_agent=_agent()))


def test_approve_refuses_a_bad_gate_or_decision(launch_home):
    _started_run(launch_home)
    assert "error" in json.loads(
        tool.web_designer_approve(RUN_ID, "build", "approve"))
    assert "error" in json.loads(
        tool.web_designer_approve(RUN_ID, "brief", "maybe"))
    assert "error" in json.loads(
        tool.web_designer_approve("no-such-run", "brief", "approve"))


def test_stop_halts_a_running_run_without_the_machine(launch_home):
    _started_run(launch_home)
    assert "gateway.web_designer.machine" not in sys.modules
    result = json.loads(tool.web_designer_stop(RUN_ID))
    assert result["status"] == "stopped"
    assert load_run(launch_home, RUN_ID).status == "stopped"


def test_stop_uses_the_machine_when_present(launch_home, monkeypatch):
    _started_run(launch_home)
    calls = []

    def stop_run(record, *, by):
        from gateway.web_designer import store

        calls.append(by)
        return store.transition(record, "stopped", reason=f"stopped by {by}")

    _install(monkeypatch, "gateway.web_designer.machine", stop_run=stop_run)
    result = json.loads(tool.web_designer_stop(RUN_ID, parent_agent=_agent()))
    assert result["status"] == "stopped"
    assert calls == ["111"]


def test_stop_of_an_ended_run_reports_its_status(launch_home):
    _started_run(launch_home)
    tool.web_designer_stop(RUN_ID)
    result = json.loads(tool.web_designer_stop(RUN_ID))
    assert result["status"] == "stopped"
    assert result["note"] == "Run already ended."


def test_judge_returns_the_verdict(launch_home, monkeypatch):
    record = _started_run(launch_home)
    record.brief = {"problem": "Sell jam"}
    save_run(record)
    verdict = Verdict(judger="a11y", passed=False, findings=[Finding(
        rule_id="R050", judger="a11y", severity="blocker",
        location="body text", expected="contrast 4.5:1",
        actual="contrast 2.1:1", evidence="shot 1440px")],
        unchecked=["R051"])
    seen = {}

    def judge(project_dir, **kw):
        seen.update(kw)
        seen["project_dir"] = project_dir
        return verdict

    monkeypatch.setitem(sys.modules, "gateway.web_designer.judgers",
                        _fake_module("gateway.web_designer.judgers"))
    monkeypatch.setitem(sys.modules, "gateway.web_designer.judgers.a11y",
                        _fake_module("gateway.web_designer.judgers.a11y",
                                     judge=judge))
    result = json.loads(tool.web_designer_judge(RUN_ID, "a11y"))
    assert result["judger"] == "a11y"
    assert result["passed"] is False
    assert result["findings"][0]["rule_id"] == "R050"
    assert result["unchecked"] == ["R051"]
    assert result["run_id"] == RUN_ID
    assert seen["project_dir"] == record.project_dir
    assert seen["brief"] == {"problem": "Sell jam"}


def test_judge_passes_layout_and_live_inputs_to_the_judger(
        launch_home, monkeypatch):
    record = _started_run(launch_home)
    layout = {"1440": [{"name": "hero", "x": 0, "y": 0, "width": 1440,
                        "height": 600}]}
    record.stage_data = {
        "preview": {"layout": layout, "baselines": "/tmp/baselines"},
        "live_metrics": {"lcp": 1.8, "inp": 120, "cls": 0.05},
        "axe_results": {"violations": []}}
    save_run(record)
    seen = {}

    def judge(project_dir, **kw):
        seen.update(kw)
        return Verdict(judger="visual", passed=True, findings=[],
                       unchecked=[])

    monkeypatch.setitem(sys.modules, "gateway.web_designer.judgers",
                        _fake_module("gateway.web_designer.judgers"))
    monkeypatch.setitem(sys.modules, "gateway.web_designer.judgers.visual",
                        _fake_module("gateway.web_designer.judgers.visual",
                                     judge=judge))
    result = json.loads(tool.web_designer_judge(RUN_ID, "visual"))
    assert result["judger"] == "visual"
    assert seen["layout"] == layout
    assert seen["baselines"] == "/tmp/baselines"
    assert seen["live_metrics"] == {"lcp": 1.8, "inp": 120, "cls": 0.05}
    assert seen["axe_results"] == {"violations": []}


def test_judge_refuses_an_unknown_judger(launch_home):
    _started_run(launch_home)
    assert "error" in json.loads(tool.web_designer_judge(RUN_ID, "human"))
    assert "error" in json.loads(tool.web_designer_judge(RUN_ID, "taste"))


def test_judge_fails_closed_without_the_judgers_module(launch_home, monkeypatch):
    _started_run(launch_home)
    _block(monkeypatch, "gateway.web_designer.judgers.visual")
    result = json.loads(tool.web_designer_judge(RUN_ID, "visual"))
    assert "error" in result
    assert "not available yet" in result["error"]


# --- surface -----------------------------------------------------------------


class TestSurface:
    """The tools exist only inside the web-designer toolset: never core,
    deferred by default, invisible to ordinary turns."""

    def test_registered_in_the_web_designer_toolset(self):
        mapping = registry.get_tool_to_toolset_map()
        for name in tool.WEB_DESIGNER_TOOLS:
            assert mapping.get(name) == "web-designer"
        assert sorted(resolve_toolset("web-designer")) == sorted(tool.WEB_DESIGNER_TOOLS)
        assert "web-designer" in TOOLSETS

    def test_never_in_core_tools(self):
        for name in tool.WEB_DESIGNER_TOOLS:
            assert name not in _HERMES_CORE_TOOLS

    def test_deferred_by_default(self):
        defer = DEFAULT_CONFIG["tools"]["tool_search"]["defer"]
        configured = frozenset(defer)
        for name in tool.WEB_DESIGNER_TOOLS:
            assert name in defer
            assert is_deferrable_tool_name(name, configured)

    def test_invisible_to_ordinary_turns(self):
        import model_tools

        def _names(defs):
            return [(d.get("function") or {}).get("name") for d in defs]

        def _bridge(defs):
            return next(d for d in defs if _names([d])[0] == "tool_search")

        ordinary = model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-telegram"], quiet_mode=True)
        for name in tool.WEB_DESIGNER_TOOLS:
            assert name not in _names(ordinary)
            assert name not in _bridge(ordinary)["function"]["description"]

        designer = model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-telegram", "web-designer"],
            quiet_mode=True)
        for name in tool.WEB_DESIGNER_TOOLS:
            assert name not in _names(designer)
            assert name in _bridge(designer)["function"]["description"]
