"""State-machine behaviour contracts (audit 6.1-6.30): stages complete
in order and only on a valid record, a missing record refuses cleanly,
resume skips completed stages, rollback restores the last good
checkpoint, stop/fail halt with the state recorded.
"""
import json

import pytest

from gateway.web_designer import machine, store
from gateway.web_designer.machine import MachineError, StageRefused


def _valid(tokens_path):
    return {
        "intake": {"purpose": "show work", "audience": "clients",
                   "primary_action": "contact"},
        "brief": {"problem": "no site", "audience": "clients",
                  "primary_action": "contact", "success_measure": "enquiries",
                  "tone": "calm", "non_goals": ["shop"], "intent": "show work"},
        "scope_freeze": {"pages": ["home"], "features": ["contact form"],
                         "out_of_scope": ["shop"], "delivery_form": "static site"},
        "research": {"note": "three comparable portfolios",
                     "sources": ["https://example.com"]},
        "ia": {"sitemap": ["home"], "navigation": ["home"],
               "flows": ["home -> contact"]},
        "wireframe": {"pages": [{"name": "home",
                                 "sections": ["hero", "work"]}]},
        "copy": {"pages": [{"title": "Home", "body": "Hello"}],
                 "meta": {"title": "Home"}},
        "direction": {"directions": [{"name": "calm"}, {"name": "bold"}],
                      "chosen": "calm"},
        "font": {"families": ["Inter"], "scale": {"base": 16}},
        "colour": {"roles": {"text": "#111111"},
                   "contrast_table": [{"pair": "text/bg", "ratio": 7.0}]},
        "theme": {"default": "light", "surfaces": {"base": "#ffffff"}},
        "icon": {"icon_set": "Lucide"},
        "tokens": {"tokens_path": tokens_path},
        "components": {"inventory": [{"name": "button"}]},
        "stack": {"stack": "HTML/CSS/JS", "files": ["index.html"],
                  "commands": ["python3 -m http.server"]},
        "build": {"files": ["index.html"], "build_log": "ok",
                  "token_drift": []},
        "preview": {"url_or_path": "http://127.0.0.1:8000",
                    "screenshots": {"1440": "/tmp/a.png",
                                   "768": {"unavailable": "no browser"},
                                   "375": "/tmp/c.png"},
                    "console_log": []},
        "judge_visual": {"verdict": {"judger": "visual", "passed": True,
                                     "findings": [], "unchecked": [],
                                     "measured": {}, "unjudged": False}},
        "judge_a11y": {"verdict": {"judger": "a11y", "passed": True,
                                   "findings": [], "unchecked": [],
                                   "measured": {}, "unjudged": False}},
        "judge_perf": {"verdict": {"judger": "performance", "passed": True,
                                   "findings": [], "unchecked": [],
                                   "measured": {}, "unjudged": False}},
        "judge_content": {"verdict": {"judger": "content", "passed": True,
                                      "findings": [], "unchecked": [],
                                      "measured": {}, "unjudged": False}},
        "judge_responsive": {"verdict": {"judger": "responsive", "passed": True,
                                         "findings": [], "unchecked": [],
                                         "measured": {}, "unjudged": False}},
        "fix_loop": {"iterations": 1, "remaining": []},
        "checkpoints": {"decisions": [{"gate": "brief", "decision": "approve"}]},
        "delivery": {"package": {"manifest": {"files": ["index.html"]}},
                     "verdict_summary": {"passed": True}},
        "revisions": {"classification": "fix"},
    }


def _running_record(tmp_path):
    home = tmp_path / "home"
    record = store.create_run(home=str(home), thread_id="t1",
                              user_id="u1", chat_id="c1")
    store.start_run(record)
    store.complete_stage(record, "load_gate", data={"decision": "load"})
    record.stage = "intake"
    store.save_run(record)
    return record


def _advance_through(record, valid, *stages):
    for stage in stages:
        machine.advance(record, stage, data=valid.get(stage))


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"color": {"text": {"$value": "#111111"}}}),
                    encoding="utf-8")
    return str(path)


def test_stages_complete_in_order_and_resume_skips_them(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze")
    assert record.stage == "research"
    loaded, nxt = machine.resume(str(tmp_path / "home"), record.run_id)
    assert (loaded.run_id, nxt) == (record.run_id, "research")


def test_an_out_of_order_stage_is_refused(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    with pytest.raises(MachineError):
        machine.advance(record, "brief", data=_valid(tokens_file)["brief"])


def test_a_stage_without_its_record_refuses_and_completes_nothing(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    with pytest.raises(StageRefused, match="purpose"):
        machine.advance(record, "intake", data={"audience": "clients"})
    assert record.completed_stages == ["load_gate"]


def test_research_without_a_note_and_copy_with_verify_flags_refuse(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze")
    with pytest.raises(StageRefused, match="note"):
        machine.advance(record, "research", data={"sources": ["https://x.example"]})
    _advance_through(record, valid, "research", "ia", "wireframe")
    bad_copy = {"pages": [{"title": "Home", "body": "Costs $9 [verify]"}],
                "meta": {"title": "Home"}}
    with pytest.raises(StageRefused, match="\\[verify\\]"):
        machine.advance(record, "copy", data=bad_copy)


def test_direction_and_colour_rules_are_enforced(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze",
                     "research", "ia", "wireframe", "copy")
    with pytest.raises(StageRefused):
        machine.advance(record, "direction",
                        data={"directions": [{"name": "only"}], "chosen": "only"})
    with pytest.raises(StageRefused):
        machine.advance(record, "direction",
                        data={"directions": [{"name": "a"}, {"name": "b"}],
                              "chosen": "c"})
    machine.advance(record, "direction", data=valid["direction"])
    machine.advance(record, "font", data=valid["font"])
    with pytest.raises(StageRefused, match="AA"):
        machine.advance(record, "colour",
                        data={"roles": {"text": "#777777"},
                              "contrast_table": [{"pair": "t/bg", "ratio": 2.0}]})


def test_tokens_stage_needs_a_real_valid_file(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze",
                     "research", "ia", "wireframe", "copy", "direction",
                     "font", "colour", "theme", "icon")
    with pytest.raises(StageRefused):
        machine.advance(record, "tokens",
                        data={"tokens_path": str(tmp_path / "missing.json")})
    machine.advance(record, "tokens", data=valid["tokens"])
    assert record.tokens_path == tokens_file


def test_a_judge_verdict_for_the_wrong_judger_refuses(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze",
                     "research", "ia", "wireframe", "copy", "direction",
                     "font", "colour", "theme", "icon", "tokens",
                     "components", "stack", "build", "preview")
    wrong = {"verdict": {"judger": "a11y", "passed": True, "findings": [],
                         "unchecked": [], "measured": {}, "unjudged": False}}
    with pytest.raises(StageRefused):
        machine.advance(record, "judge_visual", data=wrong)


def test_machine_level_stages_complete_with_generated_data(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze",
                     "research", "ia", "wireframe", "copy", "direction",
                     "font", "colour", "theme", "icon", "tokens",
                     "components", "stack", "build", "preview",
                     "judge_visual", "judge_a11y", "judge_perf",
                     "judge_content", "judge_responsive", "fix_loop",
                     "checkpoints", "delivery")
    machine.advance(record, "checkpointing")
    assert record.stage_data["checkpointing"]["checkpoint_written"] is True
    machine.advance(record, "rollback")
    machine.advance(record, "revisions", data=valid["revisions"])
    machine.advance(record, "routing_audit")
    assert record.stage_data["routing_audit"]["audit_entries"] > 0
    _, nxt = machine.resume(str(tmp_path / "home"), record.run_id)
    assert nxt is None


def test_rollback_restores_the_last_good_checkpoint(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    _advance_through(record, valid, "intake", "brief", "scope_freeze",
                     "research")
    machine.rollback(record, "brief")
    assert record.completed_stages == ["load_gate", "intake", "brief"]
    assert record.stage == "scope_freeze"
    assert "research" not in record.stage_data
    audit = store.read_audit(record)
    assert any(e.get("event") == "rollback" for e in audit)
    machine.advance(record, "scope_freeze", data=valid["scope_freeze"])
    assert record.stage == "research"


def test_stop_and_fail_halt_with_the_state_recorded(tmp_path, tokens_file):
    record = _running_record(tmp_path)
    valid = _valid(tokens_file)
    machine.advance(record, "intake", data=valid["intake"])
    machine.stop_run(record, by="owner")
    assert record.status == "stopped"
    with pytest.raises(MachineError):
        machine.advance(record, "brief", data=valid["brief"])
    other = _running_record(tmp_path)
    machine.advance(other, "intake", data=valid["intake"])
    machine.fail_run(other, "build crashed")
    assert other.status == "failed"
    assert other.stage_data["failure"]["error"] == "build crashed"


def test_a_run_parked_at_a_checkpoint_can_fail(tmp_path):
    """A run waiting on a human gate must still be failable: a crash or
    boot reset while parked at a checkpoint lands in ``failed``, not in
    an illegal-transition error."""
    from gateway.web_designer.types import transition_allowed

    assert transition_allowed("awaiting_checkpoint", "failed") is True
    record = _running_record(tmp_path)
    store.transition(record, "awaiting_checkpoint", reason="brief gate")
    machine.fail_run(record, "host restarted while parked")
    assert record.status == "failed"
    assert record.stage_data["failure"]["error"] == "host restarted while parked"
