"""Rich /insights card: rounded headline tokens, highlighted leaders, tips, share summary.

Covers ``InsightsEngine.format_gateway_rich`` and ``share_summary`` in
``agent/insights.py``. The plain-text ``format_gateway`` contract is unchanged —
it still carries the exact token figure — so the old flat format's tests keep
passing untouched.
"""

import time

import pytest

from agent.insights import InsightsEngine, _round_tokens
from hermes_state import SessionDB
from plugins.platforms.telegram.command_cards import _EMOJI_RE


def _overview(**overrides):
    base = {
        "total_sessions": 4, "total_messages": 12, "total_tool_calls": 9,
        "total_input_tokens": 120_000_000, "total_output_tokens": 35_553_345,
        "total_cache_read_tokens": 0, "total_cache_write_tokens": 0,
        "total_tokens": 155_553_345, "estimated_cost": 0.0, "actual_cost": 0.0,
        "total_hours": 2.5, "avg_session_duration": 2250.0,
        "avg_messages_per_session": 3.0, "avg_tokens_per_session": 38_888_336.25,
        "user_messages": 6, "assistant_messages": 6, "tool_messages": 0,
        "date_range_start": 1772000000.0, "date_range_end": 1774500000.0,
        "models_with_pricing": [], "models_without_pricing": [],
        "unknown_cost_sessions": 0, "included_cost_sessions": 0,
    }
    base.update(overrides)
    return base


def _report(**overrides):
    base = {
        "days": 30, "source_filter": None, "empty": False, "generated_at": time.time(),
        "overview": _overview(),
        "models": [{"model": "claude-sonnet-4-20250514", "sessions": 3, "total_tokens": 140_000_000,
                    "input_tokens": 110_000_000, "output_tokens": 30_000_000,
                    "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
                    "api_calls": 10, "tool_calls": 8, "cost": 0.0, "actual_cost": 0.0,
                    "cost_status": "unknown", "has_pricing": False},
                   {"model": "gpt-4o", "sessions": 1, "total_tokens": 15_553_345,
                    "input_tokens": 10_000_000, "output_tokens": 5_553_345,
                    "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
                    "api_calls": 2, "tool_calls": 1, "cost": 0.0, "actual_cost": 0.0,
                    "cost_status": "unknown", "has_pricing": False}],
        "platforms": [{"platform": "telegram", "sessions": 3, "messages": 9,
                       "input_tokens": 100_000_000, "output_tokens": 30_000_000,
                       "cache_read_tokens": 0, "cache_write_tokens": 0,
                       "total_tokens": 130_000_000, "tool_calls": 7},
                      {"platform": "cli", "sessions": 1, "messages": 3,
                       "input_tokens": 20_000_000, "output_tokens": 5_553_345,
                       "cache_read_tokens": 0, "cache_write_tokens": 0,
                       "total_tokens": 25_553_345, "tool_calls": 2}],
        "tools": [{"tool": "search_files", "count": 5, "percentage": 55.6},
                  {"tool": "terminal", "count": 4, "percentage": 44.4}],
        "skills": {"summary": {"total_skill_loads": 2, "total_skill_edits": 1,
                                "total_skill_actions": 3, "distinct_skills_used": 2},
                   "top_skills": [{"skill": "github-pr-workflow", "view_count": 2,
                                    "manage_count": 0, "total_count": 2, "percentage": 66.7,
                                    "last_used_at": 1774400000.0},
                                   {"skill": "systematic-debugging", "view_count": 0,
                                    "manage_count": 1, "total_count": 1, "percentage": 33.3,
                                    "last_used_at": None}]},
        "activity": {"by_day": [{"day": d, "count": c} for d, c in
                                zip(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], [1, 0, 2, 0, 1, 0, 0])],
                     "by_hour": [{"hour": h, "count": 3 if h == 10 else 0} for h in range(24)],
                     "busiest_day": {"day": "Wed", "count": 2},
                     "busiest_hour": {"hour": 10, "count": 3},
                     "active_days": 3, "max_streak": 2},
        "top_sessions": [{"label": "Most messages", "session_id": "s1",
                          "value": "8 msgs", "date": "Mar 01"}],
    }
    base.update(overrides)
    return base


@pytest.fixture()
def engine():
    return InsightsEngine.__new__(InsightsEngine)


class TestRoundTokens:
    @pytest.mark.parametrize("n,expected", [
        (0, "0"), (742, "742"), (999, "999"),
        (1000, "≈1K"), (1500, "≈1.5K"), (12_345, "≈12.3K"),
        (999_999, "≈1M"), (1_000_000, "≈1M"),
        (155_553_345, "≈155.6M"), (2_400_000_000, "≈2.4B"),
    ])
    def test_magnitude_units(self, n, expected):
        assert _round_tokens(n) == expected

    def test_negative_and_none_degrade_to_zero(self):
        assert _round_tokens(-5) == "0"
        assert _round_tokens(None) == "0"


class TestShareSummary:
    def test_headline_numbers_rounded_and_shareable(self, engine):
        summary = engine.share_summary(_report())
        assert "4 sessions" in summary
        assert "12 messages" in summary
        assert "≈155.6M tokens" in summary
        assert "claude-sonnet-4-20250514" in summary
        assert "155,553,345" not in summary
        assert len(summary) <= 256  # copy-button limit

    def test_empty_report(self, engine):
        summary = engine.share_summary({"days": 30, "empty": True})
        assert "no sessions" in summary


class TestFormatGatewayRich:
    def test_headline_tokens_rounded_exact_kept_in_detail(self, engine):
        html = engine.format_gateway_rich(_report())
        assert "<strong>≈155.6M</strong>" in html
        # The exact figure survives, but only once, in the detail line — never as the headline.
        assert html.count("155,553,345") == 1
        assert "Exact total: 155,553,345 tokens" in html

    def test_sections_divided_and_index_italic(self, engine):
        html = engine.format_gateway_rich(_report())
        assert "<hr>" in html
        assert "<h3>Models</h3>" in html
        assert "<h3>Top tools</h3>" in html
        assert "<h3>When you work</h3>" in html
        assert "<p><i>Insights · Last 30 days · 4 sessions · 3 active days</i></p>" in html

    def test_leader_highlighted_first_in_each_list(self, engine):
        html = engine.format_gateway_rich(_report())
        assert "<li><strong>claude-sonnet-4-20250514" in html
        assert "<li>gpt-4o" in html
        assert html.index("claude-sonnet-4-20250514") < html.index("gpt-4o")
        assert "<li><strong>search_files" in html
        assert "<li>terminal" in html

    def test_dominant_model_tip_present(self, engine):
        # Top model carries 140M of 155.6M tokens — the tip must say so.
        html = engine.format_gateway_rich(_report())
        assert "Tip: claude-sonnet-4-20250514 carries 90% of your tokens" in html

    def test_no_tip_when_usage_is_spread(self, engine):
        report = _report()
        report["models"] = [
            {**report["models"][0], "total_tokens": 77_776_672},
            {**report["models"][1], "total_tokens": 77_776_673},
        ]
        report["tools"] = [{"tool": "search_files", "count": 5, "percentage": 20.0},
                           {"tool": "terminal", "count": 20, "percentage": 80.0}]
        html = engine.format_gateway_rich(report)
        assert "carries" not in html

    def test_top_tool_tip_present(self, engine):
        html = engine.format_gateway_rich(_report())
        assert "Tip: search_files is 56% of all tool calls" in html

    def test_busiest_day_tip_present(self, engine):
        # Wednesday holds 2 of 4 sessions — half the week on one day.
        html = engine.format_gateway_rich(_report())
        assert "Tip: Weds carry 50% of your sessions" in html

    def test_empty_report(self, engine):
        html = engine.format_gateway_rich({"days": 7, "empty": True})
        assert "No sessions found in the last 7 days." in html

    def test_no_emoji_in_rich_output(self, engine):
        assert _EMOJI_RE.search(engine.format_gateway_rich(_report())) is None

    def test_plain_gateway_still_carries_exact_figure(self, engine):
        # The plain-text path is untouched: it keeps the literal count.
        assert "155,553,345" in engine.format_gateway(_report())


class TestRichEndToEnd:
    def test_generate_then_rich_rounds_big_totals(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "rich.db")
        try:
            db.create_session(session_id="big", source="cli", model="gpt-4o")
            db.update_token_counts("big", input_tokens=150_000_000, output_tokens=5_553_345)
            db._conn.commit()
            engine = InsightsEngine(db)
            report = engine.generate(days=30)
            html = engine.format_gateway_rich(report)
            assert "≈155.6M" in html
            assert engine.share_summary(report).endswith("top model gpt-4o.")
        finally:
            db.close()
