"""Behaviour contracts for ``gateway/web_designer/judgers/checkers.py``.

Each checker runs locally on a tmp_path fixture project: no network, no
binaries. Lighthouse and axe return unavailable on this host because
neither binary is installed — and no numbers or violations are invented.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from gateway.web_designer.judgers import checkers
from gateway.web_designer.judgers.common import load_project


def _project(tmp_path: Path, *, html: str = "", css: str = "") -> object:
    if html:
        (tmp_path / "index.html").write_text(html)
    if css:
        (tmp_path / "style.css").write_text(css)
    project = load_project(tmp_path)
    assert project is not None
    return project


GOOD_HTML = """<!doctype html><html><head><title>T</title></head>
<body><h1>Hi</h1><p>Body copy.</p></body></html>
"""


def test_css_validity_bad_file(tmp_path):
    project = _project(tmp_path, css="body { colr: red; font-size: 16px; }\n")
    result = checkers.check_css_validity(project)
    assert result["available"] is True
    assert "colr" in result["measured"]["unknown_properties"]
    assert result["measured"]["css_errors"] >= 1
    assert any(v["rule_id"] is None and "colr" in v["actual"]
               for v in result["violations"])


def test_css_validity_good_file(tmp_path):
    project = _project(
        tmp_path,
        css=":root { --brand: #2563eb; }\n"
            "body { color: #111827; -webkit-font-smoothing: antialiased; }\n")
    result = checkers.check_css_validity(project)
    assert result["violations"] == []
    assert result["measured"]["css_errors"] == 0


def test_css_validity_unbalanced_braces(tmp_path):
    project = _project(tmp_path, css="body { color: red;\n")
    result = checkers.check_css_validity(project)
    assert result["measured"]["css_errors"] >= 1
    assert any("brace" in v["actual"] for v in result["violations"])


def test_html_parse_flags_unclosed_tag(tmp_path):
    project = _project(
        tmp_path,
        html="<!doctype html><html><body><div><p>Text</div></body></html>\n")
    result = checkers.check_html_parse(project)
    assert result["available"] is True
    assert result["measured"]["html_parse_errors"] >= 1
    assert all(v["rule_id"] is None for v in result["violations"])


def test_html_parse_good_document(tmp_path):
    project = _project(tmp_path, html=GOOD_HTML)
    result = checkers.check_html_parse(project)
    assert result["violations"] == []
    assert result["measured"]["html_parse_errors"] == 0


def test_links_broken_internal_link_is_r107(tmp_path):
    project = _project(
        tmp_path,
        html="<!doctype html><html><body>"
             "<a href=\"missing.html\">gone</a>"
             "<a href=\"https://example.com/\">external</a>"
             "<img src=\"gone.png\">"
             "</body></html>\n")
    result = checkers.check_links(project)
    assert result["available"] is True
    assert all(v["rule_id"] == "R107" for v in result["violations"])
    assert len(result["violations"]) == 2
    assert result["measured"]["external_links_unchecked"] == [
        "https://example.com/"]


def test_links_resolving_internal_link_passes(tmp_path):
    (tmp_path / "about.html").write_text("<html><body>About</body></html>")
    project = _project(
        tmp_path,
        html="<!doctype html><html><body>"
             "<a href=\"about.html\">about</a></body></html>\n")
    result = checkers.check_links(project)
    assert result["violations"] == []


def test_font_sources_unknown_source_is_r013(tmp_path):
    project = _project(
        tmp_path,
        css="@font-face { font-family: X; "
            "src: url('https://evil.example.com/x.woff2'); }\n")
    result = checkers.check_font_sources(project)
    assert result["available"] is True
    assert len(result["violations"]) == 1
    assert result["violations"][0]["rule_id"] == "R013"
    assert "evil.example.com" in result["violations"][0]["evidence"]
    assert result["measured"]["font_sources"] == [
        "https://evil.example.com/x.woff2"]


def test_font_sources_allowed_sources_pass(tmp_path):
    project = _project(
        tmp_path,
        css="@import url('https://fonts.googleapis.com/css2?family=Inter');\n"
            "@font-face { font-family: Local; "
            "src: url('fonts/local.woff2'); }\n")
    result = checkers.check_font_sources(project)
    assert result["violations"] == []
    assert len(result["measured"]["font_sources"]) == 2


def test_icon_consistency_inconsistent_strokes_and_sizes(tmp_path):
    project = _project(
        tmp_path,
        html="<!doctype html><html><body>"
             "<svg width=\"24\" height=\"24\" stroke-width=\"2\">"
             "<path d=\"M0 0\"/></svg>"
             "<svg width=\"13\" height=\"13\" stroke-width=\"1.5\">"
             "<path d=\"M0 0\"/></svg>"
             "<button><svg width=\"24\" height=\"24\">"
             "<path d=\"M0 0\"/></svg></button>"
             "</body></html>\n")
    result = checkers.check_icon_consistency(project)
    assert result["available"] is True
    assert result["measured"]["icon_count"] == 3
    rules = [v["rule_id"] for v in result["violations"]]
    assert rules and all(r == "R069" for r in rules)
    assert any("stroke-width" in v["actual"] for v in result["violations"])
    assert any("13" in v["actual"] for v in result["violations"])
    assert any("accessible name" in v["expected"]
               for v in result["violations"])


def test_icon_consistency_consistent_set_passes(tmp_path):
    project = _project(
        tmp_path,
        html="<!doctype html><html><body>"
             "<svg width=\"24\" height=\"24\" stroke-width=\"2\">"
             "<path d=\"M0 0\"/></svg>"
             "<button aria-label=\"Menu\"><svg width=\"24\" height=\"24\" "
             "stroke-width=\"2\"><path d=\"M0 0\"/></svg></button>"
             "</body></html>\n")
    result = checkers.check_icon_consistency(project)
    assert result["violations"] == []
    assert result["measured"]["icon_count"] == 2


def _shot(directory: Path, width: int, colour: tuple[int, int, int]) -> None:
    Image.new("RGB", (width, 10), colour).save(directory / f"{width}.png")


def test_visual_diff_identical_screenshots_zero(tmp_path):
    baseline, current = tmp_path / "base", tmp_path / "curr"
    baseline.mkdir()
    current.mkdir()
    for width in (1440, 768, 375):
        _shot(baseline, width, (255, 255, 255))
        _shot(current, width, (255, 255, 255))
    result = checkers.diff_screenshots(baseline, current)
    assert result["available"] is True
    assert result["violations"] == []
    for width in ("1440", "768", "375"):
        assert result["measured"]["visual_diff"][width]["mismatch_pct"] == 0.0


def test_visual_diff_changed_screenshot_positive(tmp_path):
    baseline, current = tmp_path / "base", tmp_path / "curr"
    baseline.mkdir()
    current.mkdir()
    for width in (1440, 768, 375):
        _shot(baseline, width, (255, 255, 255))
        _shot(current, width, (0, 0, 0))
    result = checkers.diff_screenshots(baseline, current)
    assert result["available"] is True
    assert result["measured"]["visual_diff"]["1440"]["mismatch_pct"] > 0


def test_visual_diff_missing_baseline_unavailable(tmp_path):
    baseline, current = tmp_path / "base", tmp_path / "curr"
    baseline.mkdir()
    current.mkdir()
    _shot(current, 1440, (255, 255, 255))
    result = checkers.diff_screenshots(baseline, current)
    assert result["available"] is False
    assert result["reason"]
    assert result["measured"] == {}


def test_run_lighthouse_unavailable_without_binary(tmp_path):
    result = checkers.run_lighthouse(tmp_path)
    assert result["available"] is False
    assert result["live_metrics"] is None
    assert "lighthouse" in result["reason"].lower()


def test_run_axe_unavailable_without_binary(tmp_path):
    result = checkers.run_axe(tmp_path)
    assert result["available"] is False
    assert result["axe_results"] is None
    assert "axe" in result["reason"].lower()
