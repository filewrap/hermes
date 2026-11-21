"""Behaviour contracts for the Phase 3 judger work: R040 Fitts (static
CSS + layout boxes), R096 thumb zone (layout boxes at viewport 375),
R045 Doherty threshold (live feedback latency), and the a11y axe-results
wiring. Fixtures are small tmp_path projects in the style of
test_web_designer_judgers.py; every test asserts on verdict behaviour,
never on source text."""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.web_designer.judgers import a11y, performance, responsive
from gateway.web_designer.rules import rules_for_judger

CLEAN_CSS = """\
body { font-size: 16px; line-height: 1.5; color: #111827; background-color: #ffffff; margin: 0; font-family: Inter, system-ui; }
h1 { font-size: 32px; line-height: 1.1; color: #111827; }
.btn-primary { display: inline-block; min-height: 48px; min-width: 48px; padding: 16px 24px; background-color: #2563eb; color: #ffffff; font-size: 16px; }
button { min-height: 48px; }
a { color: #2563eb; }
a:focus-visible { outline: 3px solid #2563eb; outline-offset: 2px; }
nav a { min-height: 44px; display: inline-block; padding: 8px 16px; }
@media (min-width: 700px) { .layout { gap: 24px; } }
@media (min-width: 1100px) { .content { max-width: 66ch; } }
@media (max-width: 700px) { .btn-primary { position: fixed; bottom: calc(16px + env(safe-area-inset-bottom)); } }
"""

CLEAN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Clean</title><link rel="stylesheet" href="styles.css"></head>
<body><a class="skip-link" href="#main">Skip to content</a>
<header><nav aria-label="Primary"><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
<main id="main"><section class="hero"><h1>Clear websites for small teams</h1><p>We design fast, accessible websites.</p><a class="btn-primary" href="contact.html">Start a project</a></section></main>
<footer><p>Clean Studio.</p></footer>
<script src="app.js"></script></body></html>"""

FITTS_BAD_CSS = """\
body { font-size: 16px; line-height: 1.5; color: #111827; background-color: #ffffff; margin: 0; }
.btn-primary { display: inline-block; min-height: 20px; padding: 8px; background-color: #2563eb; color: #ffffff; }
.modal-close { position: absolute; top: 0px; right: 0px; width: 16px; height: 16px; }
@media (min-width: 700px) { .layout { gap: 24px; } }
@media (max-width: 700px) { .btn-primary { position: fixed; bottom: calc(16px + env(safe-area-inset-bottom)); } }
"""

FITTS_BAD_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Fitts</title><link rel="stylesheet" href="styles.css"></head>
<body><header><nav><a href="index.html">Home</a></nav></header>
<main><h1>Fitts</h1><button class="btn-primary">Open details</button>
<div class="modal" role="dialog"><button class="modal-close" aria-label="Close">x</button><p>Details.</p></div></main>
<footer><p>Footer.</p></footer></body></html>"""

PERF_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Perf</title></head>
<body><header><nav><a href="index.html">Home</a></nav></header>
<main><h1>Perf</h1><p>Fast.</p></main><footer><p>Footer.</p></footer></body></html>"""

AXE_RESULTS = {
    "violations": [
        {"rule": "color-contrast", "impact": "serious",
         "description": "Elements must have sufficient color contrast",
         "nodes": ["p.intro"]},
        {"rule": "color-contrast", "impact": "moderate",
         "description": "Large text must have sufficient contrast",
         "nodes": ["h1.hero"], "size": "large"},
        {"rule": "color-contrast", "impact": "moderate",
         "description": "Large text must have sufficient contrast",
         "nodes": ["h2.sub"], "size": "24px"},
        {"rule": "button-name", "impact": "critical",
         "description": "Buttons must have discernible text",
         "nodes": ["button.icon"]},
        {"rule": "link-name", "impact": "serious",
         "description": "Links must have discernible text",
         "nodes": ["a.icon"]},
        {"rule": "image-alt", "impact": "critical",
         "description": "Images must have alternate text",
         "nodes": ["img.hero"]},
        {"rule": "aria-valid-attr", "impact": "moderate",
         "description": "ARIA attributes must conform to valid values",
         "nodes": ["div[aria-label]"]},
        {"rule": "keyboard", "impact": "serious",
         "description": "Functionality must be keyboard operable",
         "nodes": ["div.tab"]},
        {"rule": "focus", "impact": "minor",
         "description": "Focus must be visible",
         "nodes": ["a.nav"]},
        {"rule": "landmark-one-main", "impact": "moderate",
         "description": "Page must have a main landmark",
         "nodes": ["body"]},
        {"rule": "heading-order", "impact": "moderate",
         "description": "Heading levels should only increment by one",
         "nodes": ["h3.sub"]},
    ],
}


@pytest.fixture
def clean_site(tmp_path: Path) -> Path:
    (tmp_path / "styles.css").write_text(CLEAN_CSS, encoding="utf-8")
    (tmp_path / "index.html").write_text(CLEAN_HTML, encoding="utf-8")
    (tmp_path / "app.js").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def fitts_bad_site(tmp_path: Path) -> Path:
    (tmp_path / "styles.css").write_text(FITTS_BAD_CSS, encoding="utf-8")
    (tmp_path / "index.html").write_text(FITTS_BAD_HTML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def perf_site(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text(PERF_HTML, encoding="utf-8")
    (tmp_path / "budgets.json").write_text(
        '{"budgets": [{"resource": "script", "budget": 200}]}',
        encoding="utf-8")
    return tmp_path


def _screenshots(tmp_path: Path) -> list[str]:
    shots = []
    for width in (1440, 768, 375):
        shot = tmp_path / f"{width}.png"
        shot.write_bytes(b"fake-png")
        shots.append(str(shot))
    return shots


def _rule_ids(verdict) -> set[str]:
    return {f.rule_id for f in verdict.findings}


def _findings(verdict, rule_id: str, severity: str | None = None):
    return [f for f in verdict.findings
            if f.rule_id == rule_id
            and (severity is None or f.severity == severity)]


# --- R040 Fitts ---------------------------------------------------------------

def test_r040_clean_targets_pass(clean_site: Path):
    verdict = responsive.judge(clean_site)
    assert verdict.passed
    assert _findings(verdict, "R040") == []
    assert "R040" in verdict.measured["checked_rules"]


def test_r040_small_target_is_a_defect(fitts_bad_site: Path):
    verdict = responsive.judge(fitts_bad_site)
    defects = _findings(verdict, "R040", "defect")
    assert defects
    assert any("20px" in f.actual for f in defects)  # .btn-primary
    assert any("16px" in f.actual for f in defects)  # .modal-close
    assert not verdict.passed


def test_r040_corner_pinned_close_is_a_change(fitts_bad_site: Path):
    verdict = responsive.judge(fitts_bad_site)
    changes = _findings(verdict, "R040", "change")
    assert changes
    assert any("top-right" in f.actual and "16px" in f.actual
               for f in changes)
    assert any("top: 0px" in f.evidence for f in changes)


def test_r040_layout_box_size_is_measured(clean_site: Path):
    layout = {"375": [
        {"name": "logo", "x": 16, "y": 16, "width": 120, "height": 32},
        {"name": "hero", "x": 0, "y": 0, "width": 375, "height": 667},
        {"name": "primary_cta", "x": 100, "y": 590, "width": 30,
         "height": 20},
    ]}
    verdict = responsive.judge(clean_site, layout=layout)
    defects = _findings(verdict, "R040", "defect")
    assert defects
    assert any("30x20" in f.actual for f in defects)
    assert any("layout box primary_cta" in f.evidence for f in defects)
    assert not verdict.passed


def test_r040_layout_distance_close_far_from_trigger(clean_site: Path):
    layout = {"375": [
        {"name": "hero", "x": 0, "y": 0, "width": 375, "height": 667},
        {"name": "primary_cta", "x": 100, "y": 590, "width": 175,
         "height": 48},
        {"name": "close", "x": 340, "y": 8, "width": 16, "height": 16},
    ]}
    verdict = responsive.judge(clean_site, layout=layout)
    changes = _findings(verdict, "R040", "change")
    assert changes
    assert any("distance" in f.evidence for f in changes)
    assert any("primary_cta centre" in f.evidence for f in changes)


# --- R096 thumb zone ----------------------------------------------------------

def test_r096_layout_cta_in_thumb_zone_passes(clean_site: Path,
                                             tmp_path: Path):
    layout = {"375": [
        {"name": "logo", "x": 16, "y": 16, "width": 120, "height": 32},
        {"name": "hero", "x": 0, "y": 0, "width": 375, "height": 667},
        {"name": "primary_cta", "x": 100, "y": 590, "width": 175,
         "height": 48},
    ]}
    verdict = responsive.judge(clean_site, screenshots=_screenshots(tmp_path),
                               layout=layout)
    assert verdict.passed
    assert _findings(verdict, "R096") == []
    assert verdict.measured["layout_basis"] == "layout"


def test_r096_layout_cta_in_top_corner_flagged(clean_site: Path,
                                               tmp_path: Path):
    layout = {"375": [
        {"name": "hero", "x": 0, "y": 0, "width": 375, "height": 667},
        {"name": "primary_cta", "x": 300, "y": 8, "width": 60,
         "height": 48},
    ]}
    verdict = responsive.judge(clean_site, screenshots=_screenshots(tmp_path),
                               layout=layout)
    changes = _findings(verdict, "R096", "change")
    assert changes
    assert any("(330, 32)" in f.actual for f in changes)
    assert verdict.measured["layout_basis"] == "layout"


def test_r096_static_check_without_layout(clean_site: Path, tmp_path: Path):
    verdict = responsive.judge(clean_site, screenshots=_screenshots(tmp_path))
    assert verdict.passed
    assert _findings(verdict, "R096") == []
    assert verdict.measured["layout_basis"] == "css"


def test_r096_unchecked_without_screenshots_even_with_layout(
        clean_site: Path):
    layout = {"375": [
        {"name": "primary_cta", "x": 100, "y": 590, "width": 175,
         "height": 48},
    ]}
    verdict = responsive.judge(clean_site, layout=layout)
    assert "R096" in verdict.unchecked
    assert "layout_basis" not in verdict.measured


# --- R045 Doherty -------------------------------------------------------------

def test_r045_feedback_over_400ms_is_a_defect(perf_site: Path):
    verdict = performance.judge(
        perf_site, live_metrics={"feedback_latency_ms": 650})
    assert not verdict.passed
    defects = _findings(verdict, "R045", "defect")
    assert defects
    assert any("650ms" in f.actual for f in defects)
    assert verdict.measured["feedback_latency_ms"] == 650
    assert "R045" in verdict.measured["checked_rules"]
    assert "R045" not in verdict.unchecked


def test_r045_feedback_ms_alias_is_accepted(perf_site: Path):
    verdict = performance.judge(
        perf_site, live_metrics={"feedback_ms": 500})
    assert not verdict.passed
    assert _findings(verdict, "R045", "defect")
    assert verdict.measured["feedback_latency_ms"] == 500


def test_r045_feedback_under_400ms_passes(perf_site: Path):
    verdict = performance.judge(
        perf_site, live_metrics={"feedback_latency_ms": 250})
    assert verdict.passed
    assert _findings(verdict, "R045") == []
    assert verdict.measured["feedback_latency_ms"] == 250
    assert "R045" in verdict.measured["checked_rules"]


def test_r045_unchecked_without_feedback_metric(perf_site: Path):
    verdict = performance.judge(perf_site, live_metrics={"lcp": 1.8})
    assert "R045" in verdict.unchecked
    assert "feedback_latency_ms" not in verdict.measured
    assert "R045" not in verdict.measured["checked_rules"]


def test_r045_inp_is_not_a_feedback_latency_proxy(perf_site: Path):
    verdict = performance.judge(perf_site, live_metrics={"inp": 120})
    assert "R045" in verdict.unchecked
    assert "feedback_latency_ms" not in verdict.measured


# --- axe-results wiring -------------------------------------------------------

def test_axe_violations_map_to_audit_rules(clean_site: Path):
    verdict = a11y.judge(clean_site, axe_results=AXE_RESULTS)
    assert not verdict.passed
    assert verdict.measured["axe_violations"] == 11
    axe = [f for f in verdict.findings if f.evidence.startswith("axe ")]
    assert {f.rule_id for f in axe} == {
        "R072", "R073", "R075", "R076", "R077", "R078"}
    by_rule = {f.rule_id: f for f in axe}
    assert by_rule["R072"].severity == "blocker"  # serious contrast
    assert by_rule["R078"].severity == "blocker"  # critical image-alt
    assert by_rule["R075"].severity == "blocker"  # serious keyboard
    assert by_rule["R076"].severity == "change"  # minor focus
    assert len([f for f in axe if f.rule_id == "R073"]) == 2  # large + 24px
    assert len([f for f in axe if f.rule_id == "R077"]) == 5  # names/aria/landmark/heading
    assert by_rule["R072"].location == "p.intro"
    assert "color-contrast" in by_rule["R072"].evidence


def test_axe_empty_results_record_zero(clean_site: Path):
    verdict = a11y.judge(clean_site, axe_results={"violations": []})
    assert verdict.passed
    assert verdict.measured["axe_violations"] == 0
    assert not [f for f in verdict.findings
                if f.evidence.startswith("axe ")]


def test_axe_none_leaves_behaviour_unchanged(clean_site: Path):
    verdict = a11y.judge(clean_site)
    assert verdict.passed
    assert "axe_violations" not in verdict.measured


def test_axe_unknown_rule_id_is_skipped_but_counted(clean_site: Path):
    verdict = a11y.judge(clean_site, axe_results={
        "violations": [{"rule": "some-future-rule", "impact": "critical",
                        "description": "Not in the audit mapping",
                        "nodes": ["div.x"]}]})
    assert verdict.measured["axe_violations"] == 1
    assert not [f for f in verdict.findings
                if f.evidence.startswith("axe some-future-rule")]


# --- coverage invariants ------------------------------------------------------

def test_coverage_responsive_with_layout_and_screenshots(clean_site: Path,
                                                        tmp_path: Path):
    layout = {"375": [
        {"name": "hero", "x": 0, "y": 0, "width": 375, "height": 667},
        {"name": "primary_cta", "x": 100, "y": 590, "width": 175,
         "height": 48},
    ]}
    verdict = responsive.judge(clean_site, screenshots=_screenshots(tmp_path),
                               layout=layout)
    owned = {r["id"] for r in rules_for_judger("responsive")}
    covered = set(verdict.measured["checked_rules"]) | set(verdict.unchecked)
    assert covered == owned


def test_coverage_performance_with_feedback_metric(perf_site: Path):
    verdict = performance.judge(
        perf_site, live_metrics={"feedback_latency_ms": 250})
    owned = {r["id"] for r in rules_for_judger("performance")}
    covered = set(verdict.measured["checked_rules"]) | set(verdict.unchecked)
    assert covered == owned


def test_coverage_a11y_with_axe_results(clean_site: Path):
    verdict = a11y.judge(clean_site, axe_results=AXE_RESULTS)
    owned = {r["id"] for r in rules_for_judger("a11y")}
    covered = set(verdict.measured["checked_rules"]) | set(verdict.unchecked)
    assert covered == owned
