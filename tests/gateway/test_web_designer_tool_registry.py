"""Behaviour contracts for the web-designer tool registry (Phase 3).

The registry holds all 64 audit tools with their verbatim audit text;
exactly nine are live slots with a checker, the other 55 are reference
entries. A reference entry is never claimed as a live check.
"""
from __future__ import annotations

from gateway.web_designer.tool_registry import (
    BUCKETS, TOOLS, available_live_checkers, checker_for, get_tool,
    live_checker_available, live_checkers, registry_summary, tools_by_bucket,
    tools_with_checkers,
)

LIVE_NAMES = {
    "W3C Markup Validation Service", "W3C CSS Validation Service",
    "Dead Link Checker", "Lighthouse", "axe DevTools",
    "WebAIM Contrast Checker", "BackstopJS", "Google Fonts", "Fontshare",
}

CHECKERS = {
    "W3C Markup Validation Service": "html_parse",
    "W3C CSS Validation Service": "css_validity",
    "Dead Link Checker": "link_check",
    "Lighthouse": "lighthouse",
    "axe DevTools": "axe",
    "WebAIM Contrast Checker": "contrast",
    "BackstopJS": "visual_diff",
    "Google Fonts": "font_sources",
    "Fontshare": "font_sources",
}


def test_registry_has_all_64_tools_with_unique_names():
    assert len(TOOLS) == 64
    names = [t["name"] for t in TOOLS]
    assert len(set(names)) == 64
    assert set(BUCKETS) == {t["bucket"] for t in TOOLS}


def test_every_entry_has_complete_fields():
    for tool in TOOLS:
        for key in ("name", "url", "bucket", "license", "kind", "what",
                    "hermes_use"):
            assert tool[key], f"{tool['name']} is missing {key}"
        assert tool["kind"] in {"live", "reference"}
        assert tool["url"].startswith("http")


def test_live_set_is_exactly_the_nine_slots():
    live = {t["name"] for t in TOOLS if t["kind"] == "live"}
    assert live == LIVE_NAMES
    assert {t["name"] for t in live_checkers()} == LIVE_NAMES
    assert {t["name"] for t in tools_with_checkers()} == LIVE_NAMES


def test_checker_for_maps_live_slots_and_none_for_reference():
    for name, checker in CHECKERS.items():
        assert checker_for(name) == checker
    assert checker_for("Awwwards") is None
    assert checker_for("Sentry") is None
    assert checker_for("no such tool") is None


def test_live_entries_carry_requires_or_live_via():
    lighthouse = get_tool("Lighthouse")
    assert lighthouse["requires"] == "lighthouse binary on PATH"
    assert get_tool("axe DevTools")["requires"] == "axe binary on PATH"
    backstop = get_tool("BackstopJS")
    assert backstop["live_via"] == "local visual diff (PIL)"
    # Pure-local slots need no binary.
    assert get_tool("WebAIM Contrast Checker")["requires"] is None
    assert get_tool("Dead Link Checker")["requires"] is None


def test_live_equivalents_are_only_the_three_mappings():
    equivalents = {t["name"]: t["live_equivalent"] for t in TOOLS
                   if "live_equivalent" in t}
    assert equivalents == {
        "PageSpeed Insights": "Lighthouse",
        "WAVE": "axe DevTools",
        "Responsinator": "responsive judger (1440/768/375 sweep)",
    }


def test_get_tool_and_tools_by_bucket():
    tool = get_tool("lighthouse")
    assert tool is not None and tool["name"] == "Lighthouse"
    assert get_tool("no such tool") is None
    fonts = tools_by_bucket("fonts")
    assert [t["name"] for t in fonts] == [
        "Google Fonts", "Fontshare", "Fontjoy"]
    assert tools_by_bucket("no such bucket") == []


def test_registry_summary_counts():
    summary = registry_summary()
    assert summary["total"] == 64
    assert summary["live"] == 9
    assert summary["reference"] == 55
    by_bucket = summary["by_bucket"]
    assert by_bucket["validators/bug finders"] == {"live": 3, "reference": 1}
    assert by_bucket["fonts"] == {"live": 2, "reference": 1}
    assert by_bucket["visual-diff"] == {"live": 1, "reference": 3}
    assert by_bucket["performance"] == {"live": 1, "reference": 3}
    assert by_bucket["accessibility"] == {"live": 2, "reference": 3}
    assert sum(v["live"] for v in by_bucket.values()) == 9
    assert sum(v["reference"] for v in by_bucket.values()) == 55


def test_binary_slots_unavailable_on_this_host():
    # Neither binary is installed here; the slots stay reference behaviour.
    assert live_checker_available("Lighthouse") is False
    assert live_checker_available("axe DevTools") is False


def test_pure_local_slots_available_on_this_host():
    for name in ("W3C Markup Validation Service",
                 "W3C CSS Validation Service", "Dead Link Checker",
                 "WebAIM Contrast Checker", "BackstopJS", "Google Fonts",
                 "Fontshare"):
        assert live_checker_available(name) is True, name
    available = {t["name"] for t in available_live_checkers()}
    assert available == LIVE_NAMES - {"Lighthouse", "axe DevTools"}


def test_reference_entries_are_never_available():
    assert live_checker_available("Awwwards") is False
    assert live_checker_available("PageSpeed Insights") is False
    assert live_checker_available("no such tool") is False
