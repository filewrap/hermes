"""F5 decision_evaluate: contract tests through the real registry dispatch path.

Covers the invariants, not counts: the tool registers under the ``decision``
toolset; ``decisionModel`` parses as "provider/model"; the tool is unavailable
without it; a configured call sends ONLY the supplied state and questions to
the decision model; abstention is explicit; a missing probability is null,
never inferred.
"""
import json
from types import SimpleNamespace

import pytest

import hermes_cli.config as hc
from tools import decision_tool
from tools.registry import registry


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _write_config(home, text):
    (home / "config.yaml").write_text(text)
    hc._LOAD_CONFIG_CACHE.clear()


def test_tool_registers_in_decision_toolset():
    entry = registry.get_entry("decision_evaluate")
    assert entry is not None
    assert entry.toolset == "decision"
    assert registry.get_toolset_for_tool("decision_evaluate") == "decision"
    schema = entry.schema
    assert isinstance(schema.get("parameters"), dict)
    assert set(schema["parameters"]["required"]) == {"state", "questions"}
    assert "decisionModel" in schema["description"]
    assert "never authorize" in schema["description"]


def test_resolve_decision_model():
    assert decision_tool.resolve_decision_model({"decisionModel": "openrouter/google/gemini-3-flash-preview"}) == (
        "openrouter", "google/gemini-3-flash-preview")
    assert decision_tool.resolve_decision_model({"decisionModel": "  nous/gpt-5  "}) == ("nous", "gpt-5")
    assert decision_tool.resolve_decision_model({"decisionModel": ""}) == (None, None)
    assert decision_tool.resolve_decision_model({"decisionModel": "noprovider"}) == (None, None)
    assert decision_tool.resolve_decision_model({"decisionModel": "/only-model"}) == (None, None)
    assert decision_tool.resolve_decision_model({}) == (None, None)
    assert decision_tool.resolve_decision_model({"decisionModel": 42}) == (None, None)


def test_unavailable_without_decision_model(config_home):
    _write_config(config_home, "model:\n  default: test-model\n")
    assert decision_tool.check_decision_requirements() is False
    result = json.loads(decision_tool.decision_evaluate(
        state="some claim",
        questions=[{"id": "q1", "type": "boolean", "question": "Is this true?"}]))
    assert result["status"] == "unavailable"
    assert "decisionModel" in result["error"]


def test_configured_check_fn_and_loader_roundtrip(config_home):
    _write_config(
        config_home,
        "model:\n  default: test-model\ndecisionModel: \"openrouter/google/gemini-3-flash-preview\"\n")
    assert hc.load_config_readonly()["decisionModel"] == "openrouter/google/gemini-3-flash-preview"
    assert decision_tool.check_decision_requirements() is True


def _fake_llm(monkeypatch, reply_text):
    seen = {}

    def fake_call_llm(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=reply_text))])

    import agent.auxiliary_client as aux

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    return seen


def test_sends_only_supplied_state_and_questions(config_home, monkeypatch):
    _write_config(
        config_home,
        "model:\n  default: test-model\ndecisionModel: \"openrouter/test-model\"\n")
    reply = json.dumps({"answers": {
        "holds": {"answer": True, "probability_true": 0.8, "reason": "stated in the evidence"},
        "pick": {"choice": "b", "confidence": 0.7, "reason": "b matches the evidence"},
        "grade": {"score": 0.6, "reason": "partially supported"},
        "gap": {"answer": None, "probability_true": None, "reason": "no evidence supplied"},
    }})
    seen = _fake_llm(monkeypatch, reply)
    result = json.loads(decision_tool.decision_evaluate(
        state={"claim": "the cache holds", "evidence": "log line 42"},
        questions=[
            {"id": "holds", "type": "boolean", "question": "Does the cache hold?"},
            {"id": "pick", "type": "choice", "question": "Which option?", "options": ["a", "b"]},
            {"id": "grade", "type": "score", "question": "How well supported?"},
            {"id": "gap", "type": "boolean", "question": "Is the gap closed?"},
        ]))
    assert result["status"] == "ok"
    assert result["model"] == "openrouter/test-model"
    assert result["advisory_only"] is True
    assert result["answers"]["holds"] == {
        "type": "boolean", "answer": True, "probability_true": 0.8,
        "abstained": False, "reason": "stated in the evidence"}
    assert result["answers"]["pick"]["choice"] == "b"
    assert result["answers"]["grade"]["score"] == 0.6
    # Explicit abstention where the evidence is missing.
    assert result["answers"]["gap"] == {
        "type": "boolean", "answer": None, "probability_true": None,
        "abstained": True, "reason": "no evidence supplied"}
    # The wire request carries only the supplied state + questions: exactly
    # two messages, and the user message parses back to the supplied payload.
    assert seen["provider"] == "openrouter" and seen["model"] == "test-model"
    assert seen["task"] == "decision"
    assert len(seen["messages"]) == 2
    sent = json.loads(seen["messages"][1]["content"])
    assert sent["state"] == {"claim": "the cache holds", "evidence": "log line 42"}
    assert [q["id"] for q in sent["questions"]] == ["holds", "pick", "grade", "gap"]
    assert "log line 42" not in seen["messages"][0]["content"]


def test_missing_probability_is_null_never_inferred(config_home, monkeypatch):
    _write_config(
        config_home,
        "model:\n  default: test-model\ndecisionModel: \"openrouter/test-model\"\n")
    reply = json.dumps({"answers": {
        "holds": {"answer": True, "reason": "clear from the state"}}})
    _fake_llm(monkeypatch, reply)
    result = json.loads(decision_tool.decision_evaluate(
        state="x",
        questions=[{"id": "holds", "type": "boolean", "question": "Is x true?"}]))
    assert result["answers"]["holds"]["probability_true"] is None
    assert result["answers"]["holds"]["answer"] is True


def test_invalid_questions_rejected(config_home):
    _write_config(
        config_home,
        "model:\n  default: test-model\ndecisionModel: \"openrouter/test-model\"\n")
    for bad in (
        [],
        [{"id": "q", "type": "boolean", "question": "ok?"},
         {"id": "q", "type": "boolean", "question": "dup id"}],
        [{"id": "q", "type": "choice", "question": "pick?", "options": ["only-one"]}],
        [{"id": "q", "type": "mystery", "question": "what?"}],
    ):
        result = json.loads(decision_tool.decision_evaluate(state="x", questions=bad))
        assert "error" in result
