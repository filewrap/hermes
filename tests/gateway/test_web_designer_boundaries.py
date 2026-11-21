"""Boundary behaviour contracts (audit 5.1-5.10, 5.16, 5.29): usage
accrues cost at a stated estimate, budgets warn then breach, fix-loop
caps stop the loop, the sandbox confines writes and sizes, approval
gates always deny, and only allowlisted licences enter the asset log.
"""
import json
from datetime import UTC, datetime, timedelta

import pytest

from gateway.web_designer import boundaries, store
from gateway.web_designer.boundaries import BoundaryViolation


def _running_record(tmp_path):
    record = store.create_run(home=str(tmp_path / "home"), thread_id="t1",
                              user_id="u1", chat_id="c1")
    store.start_run(record)
    return record


def test_usage_accrues_tokens_and_cost_at_the_stated_rate(tmp_path):
    record = _running_record(tmp_path)
    boundaries.record_usage(record, input_tokens=1000, output_tokens=500)
    assert (record.input_tokens, record.output_tokens) == (1000, 500)
    assert record.cost_usd == pytest.approx(0.015)
    with pytest.raises(ValueError):
        boundaries.record_usage(record, input_tokens=10, output_tokens=0,
                                rate_usd_per_1k=0)


def test_token_budget_warns_at_80_percent_then_breaches(tmp_path):
    record = _running_record(tmp_path)
    warnings = boundaries.check_budgets(record)
    assert warnings == []
    boundaries.record_usage(record, input_tokens=120_000, output_tokens=0)
    warnings = boundaries.check_budgets(record)
    assert warnings and "80%" in warnings[0]
    boundaries.record_usage(record, input_tokens=30_000, output_tokens=0)
    with pytest.raises(BoundaryViolation, match="token budget"):
        boundaries.check_budgets(record)


def test_cost_tool_call_and_iteration_caps_breach(tmp_path):
    record = _running_record(tmp_path)
    boundaries.record_usage(record, input_tokens=10_000, output_tokens=10_000,
                            rate_usd_per_1k=0.20)
    with pytest.raises(BoundaryViolation, match="cost budget"):
        boundaries.check_budgets(record)
    record = _running_record(tmp_path)
    record.tool_calls = 30
    with pytest.raises(BoundaryViolation, match="tool-call"):
        boundaries.check_budgets(record)
    record = _running_record(tmp_path)
    record.iterations = 20
    with pytest.raises(BoundaryViolation, match="iteration cap"):
        boundaries.check_budgets(record)


def test_the_same_tool_with_the_same_args_three_times_breaches(tmp_path):
    record = _running_record(tmp_path)
    for _ in range(3):
        boundaries.record_usage(record, input_tokens=0, output_tokens=0,
                                tool="read_file", tool_args={"path": "a.html"})
    with pytest.raises(BoundaryViolation, match="same tool"):
        boundaries.check_budgets(record)


def test_wall_clock_build_cap_breaches_after_120_minutes(tmp_path):
    record = _running_record(tmp_path)
    past = datetime.now(UTC) - timedelta(minutes=121)
    record.started_at = past.isoformat(timespec="seconds")
    with pytest.raises(BoundaryViolation, match="wall-clock build"):
        boundaries.check_budgets(record)


def test_fix_loop_cap_same_error_and_no_progress_breakers(tmp_path):
    record = _running_record(tmp_path)
    assert boundaries.check_fix_loop_cap(record, "R072@home") == 0
    record.fix_attempts["R072@home"] = 3
    with pytest.raises(BoundaryViolation, match="fix-loop cap"):
        boundaries.check_fix_loop_cap(record, "R072@home")
    record = _running_record(tmp_path)
    boundaries.check_same_error(record, "boom")
    boundaries.check_same_error(record, "boom")
    with pytest.raises(BoundaryViolation, match="same error 3x"):
        boundaries.check_same_error(record, "boom")
    record = _running_record(tmp_path)
    assert boundaries.check_no_progress(record, changed=False) == 1
    assert boundaries.check_no_progress(record, changed=True) == 0
    boundaries.check_no_progress(record, changed=False)
    boundaries.check_no_progress(record, changed=False)
    with pytest.raises(BoundaryViolation, match="no progress"):
        boundaries.check_no_progress(record, changed=False)


def test_sandbox_blocks_escapes_and_enforces_size_caps(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    summary = boundaries.check_sandbox(project)
    assert summary["files"] == 1
    with pytest.raises(BoundaryViolation, match="escapes"):
        boundaries.check_path_within(project, tmp_path / "outside.txt")
    big = project / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(BoundaryViolation, match="over 2 MB"):
        boundaries.check_sandbox(project)
    big.unlink()
    for i in range(201):
        (project / f"f{i}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BoundaryViolation, match="file count"):
        boundaries.check_sandbox(project)


def test_never_without_approval_gates_always_deny(tmp_path):
    for action in ("publish", "deploy", "spend", "pii", "contact",
                   "dns", "delete", "destructive"):
        result = boundaries.require_approval(action, detail="example.com")
        assert result["approved"] is False
        assert "approval" in result["reason"]
        assert "example.com" in result["reason"]


def test_daily_cost_cap_accumulates_across_runs(tmp_path):
    """Audit 5.8: $10/day across every run under one home. Six $1.80
    builds stay under the $2 per-build cap individually but total
    $10.80 for the day, so the sixth run breaches; five ($9.00) pass."""
    home = tmp_path / "home"
    for i in range(5):
        record = store.create_run(home=str(home), thread_id=f"t{i}",
                                  user_id="u1", chat_id="c1")
        store.start_run(record)
        boundaries.record_usage(record, input_tokens=1800, output_tokens=0,
                                rate_usd_per_1k=1.0)
        assert boundaries.check_budgets(record) == []
    assert boundaries.read_daily_cost(str(home)) == pytest.approx(9.0)
    sixth = store.create_run(home=str(home), thread_id="t5",
                             user_id="u1", chat_id="c1")
    store.start_run(sixth)
    boundaries.record_usage(sixth, input_tokens=1800, output_tokens=0,
                            rate_usd_per_1k=1.0)
    with pytest.raises(BoundaryViolation, match="daily cost"):
        boundaries.check_budgets(sixth)


def test_daily_cost_cap_ignores_yesterday(tmp_path):
    """The daily ledger is per calendar day (Asia/Kolkata): yesterday's
    spend never counts against today."""
    from zoneinfo import ZoneInfo

    home = tmp_path / "home"
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    yesterday = today - timedelta(days=1)
    ledger_dir = home / "web-designer" / "daily_cost"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / f"{yesterday.isoformat()}.json").write_text(
        json.dumps({"date": yesterday.isoformat(), "total_cost_usd": 9.99}),
        encoding="utf-8")
    record = _running_record(tmp_path)
    boundaries.record_usage(record, input_tokens=500, output_tokens=0,
                            rate_usd_per_1k=1.0)
    assert boundaries.read_daily_cost(str(home)) == pytest.approx(0.5)
    assert boundaries.check_budgets(record) == []


def test_license_allowlist_and_asset_provenance_log(tmp_path):
    assert boundaries.check_license("https://unsplash.com/photos/x",
                                    "Unsplash License") is True
    assert boundaries.check_license("https://fonts.google.com/specimen/Inter",
                                    "SIL OFL") is True
    assert boundaries.check_license("https://lucide.dev/icons/x", "ISC") is True
    assert boundaries.check_license("https://example.com/img.png",
                                    "CC BY 4.0") is False
    assert boundaries.check_license("https://example.com/img.png",
                                    "personal use only") is False
    assert boundaries.check_license("https://example.com/img.png",
                                    "unknown licence") is False
    assert boundaries.check_license("https://flaticon.com/icon/x", "MIT") is False
    record = _running_record(tmp_path)
    entry = boundaries.record_asset(record, "https://unsplash.com/photos/x",
                                    "Unsplash License")
    assert entry["source_url"] == "https://unsplash.com/photos/x"
    assets = json.loads(
        (tmp_path / "home" / "web-designer" / record.run_id
         / "assets.json").read_text(encoding="utf-8"))
    assert assets["assets"][0]["license"] == "Unsplash License"
    with pytest.raises(BoundaryViolation, match="licence blocked"):
        boundaries.record_asset(record, "https://example.com/img.png",
                                "unknown licence")
