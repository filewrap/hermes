"""Fix-loop behaviour contracts (audit 6.24): blockers before defects,
3 cycles per issue then park-and-escalate, a fix that adds a blocker
rolls back first, and 3 no-change iterations halt the loop — never an
endless loop, never a silently dropped blocker.
"""
from gateway.web_designer import fix_loop, store
from gateway.web_designer.types import Finding, Verdict


def _finding(rule, severity, location="home"):
    return Finding(rule_id=rule, judger="visual", severity=severity,
                   location=location, expected="pass", actual="fail",
                   evidence="shot.png")


def _verdict(findings):
    return Verdict(judger="visual", passed=not findings, findings=findings)


def _running_record(tmp_path):
    record = store.create_run(home=str(tmp_path / "home"), thread_id="t1",
                              user_id="u1", chat_id="c1")
    store.start_run(record)
    return record


def test_a_blocker_fixed_in_one_cycle_is_not_escalated(tmp_path):
    record = _running_record(tmp_path)
    calls = []

    def fix(rec, finding):
        calls.append(finding["rule_id"])
        return {"changed": True}

    result = fix_loop.run_fix_loop(
        record, [_verdict([_finding("R001", "blocker")])], fix,
        rejudge_fn=lambda rec, f: [_verdict([])])
    assert result["escalated"] == []
    assert result["fixed"][0]["rule_id"] == "R001"
    assert calls == ["R001"]


def test_blockers_run_before_defects(tmp_path):
    record = _running_record(tmp_path)
    seen = []
    findings = [_finding("R009", "defect"), _finding("R001", "blocker")]

    def fix(rec, finding):
        seen.append(finding["rule_id"])
        return True

    fix_loop.run_fix_loop(record, [_verdict(findings)], fix,
                          rejudge_fn=lambda rec, f: [_verdict([])])
    assert seen == ["R001", "R009"]


def test_three_failed_cycles_park_the_issue_and_escalate(tmp_path):
    record = _running_record(tmp_path)
    calls = {"n": 0}

    def fix(rec, finding):
        calls["n"] += 1
        return True

    stuck = _finding("R072", "blocker")
    result = fix_loop.run_fix_loop(
        record, [_verdict([stuck])], fix,
        rejudge_fn=lambda rec, f: [_verdict([_finding("R072", "blocker")])])
    assert calls["n"] == 3
    assert record.fix_attempts["R072@home"] == 3
    assert result["escalated"][0]["rule_id"] == "R072"
    assert result["escalated"][0]["status"] == "parked"
    assert result["best_version"]["run_id"] == record.run_id


def test_a_fix_that_adds_a_blocker_rolls_back_first(tmp_path):
    record = _running_record(tmp_path)
    store.complete_stage(record, "build", data={"files": ["index.html"]})
    record.stage = "preview"
    store.save_run(record)

    def fix(rec, finding):
        return True

    def rejudge(rec, finding):
        return [_verdict([_finding("R001", "blocker"),
                          _finding("R099", "blocker", location="nav")])]

    result = fix_loop.run_fix_loop(
        record, [_verdict([_finding("R001", "blocker")])], fix,
        rejudge_fn=rejudge)
    audit = store.read_audit(record)
    assert any(e.get("event") == "fix_rollback" for e in audit)
    assert result["escalated"][0]["rule_id"] == "R001"


def test_three_no_change_iterations_halt_the_loop(tmp_path):
    record = _running_record(tmp_path)
    calls = {"n": 0}

    def fix(rec, finding):
        calls["n"] += 1
        return False

    stuck = _finding("R001", "blocker")
    result = fix_loop.run_fix_loop(
        record, [_verdict([stuck])], fix,
        rejudge_fn=lambda rec, f: [_verdict([_finding("R001", "blocker")])])
    assert result.get("halted") == "no_progress"
    assert calls["n"] == 3
    assert result["escalated"][0]["rule_id"] == "R001"
