"""Behaviour contracts for the Phase 3 visual-judger checks in
``gateway/web_designer/judgers/visual.py`` and the rendered measurement
in ``gateway/web_designer/judgers/rendered.py``.

Golden ratio, rule of thirds, shared axes and the Z-pattern are measured
from CSS and, when supplied, from real layout boxes; 60-30-10 is measured
from screenshot pixels with PIL when a screenshot can be read and
otherwise stays a labelled CSS estimate; the Gestalt rules R062-R070
and R093 are checked from CSS, DOM structure and paired animations.

Each test is a violating fixture (the rule fires) against a clean
fixture (it does not) built on the same base site.
"""
from __future__ import annotations

from pathlib import Path

from gateway.web_designer.judgers import visual
from gateway.web_designer.rules import rules_for_judger

BASE_CSS = """\
body { font-size: 16px; line-height: 1.5; color: #111827; background-color: #ffffff; margin: 0; font-family: Inter, system-ui; }
h1 { font-size: 40px; line-height: 1.1; color: #111827; }
h2 { font-size: 25px; line-height: 1.2; color: #111827; }
h3 { font-size: 20px; line-height: 1.2; color: #111827; }
.layout { display: grid; grid-template-columns: 8fr 5fr; gap: 24px; }
.hero { padding: 96px 24px; background-color: #ffffff; }
.card { padding: 24px; background-color: #ffffff; border: 1px solid #e5e7eb; }
.btn-primary { display: inline-block; padding: 16px 24px; background-color: #2563eb; color: #ffffff; }
a { color: #2563eb; }
label { display: block; margin-bottom: 4px; }
input, textarea, select { min-height: 48px; padding: 8px 16px; margin-bottom: 24px; }
.modal { transition: opacity 200ms ease-out; }
.modal-overlay { transition: opacity 200ms ease-out; background-color: rgba(0, 0, 0, 0.5); }
.steps { display: flex; }
footer { display: grid; grid-template-columns: 1fr 1fr 1fr; padding: 48px 24px; }
@media (prefers-reduced-motion: reduce) { * { animation-duration: 0.01ms; transition-duration: 0.01ms; } }
"""

BASE_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Acme</title><link rel="stylesheet" href="styles.css"></head>
<body>
<header><div class="logo">Acme</div><nav><a href="index.html">Home</a><a href="about.html">About</a></nav></header>
<main>
<section class="hero"><h1>Clear websites for small teams</h1><p>We design fast, accessible websites.</p><a class="btn-primary" href="contact.html">Start a project</a></section>
<section><h2>What we do</h2><p>We plan, design, and build websites with your team.</p></section>
</main><footer><div>About</div><div>Links</div><div>Contact</div></footer>
</body></html>"""

R019_CSS = """\
body { font-size: 16px; line-height: 1.5; color: #111827; background-color: #ffffff; }
.hero { background-color: #ffffff; padding: 96px 24px; }
.card { background-color: #ffffff; padding: 24px; }
.btn-primary { background-color: #dc2626; color: #ffffff; }
"""


def _site(tmp_path: Path, css: str | None = None,
          html: str | None = None) -> Path:
    (tmp_path / "styles.css").write_text(css or BASE_CSS, encoding="utf-8")
    (tmp_path / "index.html").write_text(html or BASE_HTML, encoding="utf-8")
    return tmp_path


def _rule_ids(verdict) -> set[str]:
    return {f.rule_id for f in verdict.findings}


def _finding(verdict, rule_id: str):
    return next((f for f in verdict.findings if f.rule_id == rule_id), None)


def test_layout_basis_is_css_without_layout_and_layout_with_boxes(tmp_path: Path):
    site = _site(tmp_path)
    assert visual.judge(site).measured["layout_basis"] == "css"
    layout = {"1440": [{"name": "hero", "x": 0, "y": 0,
                        "width": 1440, "height": 800}]}
    assert visual.judge(site, layout=layout).measured["layout_basis"] == "layout"


def test_checked_plus_unchecked_covers_every_visual_rule(tmp_path: Path):
    verdict = visual.judge(_site(tmp_path))
    owned = {r["id"] for r in rules_for_judger("visual")}
    covered = set(verdict.measured["checked_rules"]) | set(verdict.unchecked)
    assert covered == owned


def test_r001_golden_ratio_grid(tmp_path: Path):
    violating = BASE_CSS.replace("grid-template-columns: 8fr 5fr",
                                 "grid-template-columns: 3fr 1fr")
    finding = _finding(visual.judge(_site(tmp_path, css=violating)), "R001")
    assert finding is not None and "3.00" in finding.actual
    assert "R001" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r001_no_deliberate_proportion(tmp_path: Path):
    bare_css = ("body { font-size: 16px; line-height: 1.5; }\n"
                "h1 { font-size: 32px; }\n")
    finding = _finding(visual.judge(_site(tmp_path, css=bare_css)), "R001")
    assert finding is not None
    assert "no ratio-bearing" in finding.actual


def test_r001_layout_boxes_measure_real_ratios(tmp_path: Path):
    site = _site(tmp_path)
    bad_layout = {"1440": [
        {"name": "content", "x": 0, "y": 0, "width": 900, "height": 600},
        {"name": "sidebar", "x": 900, "y": 0, "width": 300, "height": 600}]}
    finding = _finding(visual.judge(site, layout=bad_layout), "R001")
    assert finding is not None and "3.00" in finding.actual
    good_layout = {"1440": [
        {"name": "content", "x": 0, "y": 0, "width": 800, "height": 600},
        {"name": "sidebar", "x": 800, "y": 0, "width": 500, "height": 600}]}
    assert "R001" not in _rule_ids(visual.judge(site, layout=good_layout))


def test_r002_rule_of_thirds_centred_hero(tmp_path: Path):
    violating = BASE_CSS + ".hero { text-align: center; }\n"
    assert "R002" in _rule_ids(visual.judge(_site(tmp_path, css=violating)))
    assert "R002" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r002_layout_boxes_centred_focal_points(tmp_path: Path):
    site = _site(tmp_path)
    centred = {"1440": [
        {"name": "hero", "x": 360, "y": 0, "width": 720, "height": 600},
        {"name": "primary_cta", "x": 640, "y": 320, "width": 160,
         "height": 48},
        {"name": "content", "x": 480, "y": 240, "width": 480,
         "height": 200}]}
    assert "R002" in _rule_ids(visual.judge(site, layout=centred))
    off_centre = {"1440": [
        {"name": "hero", "x": 0, "y": 0, "width": 1440, "height": 600},
        {"name": "primary_cta", "x": 1200, "y": 500, "width": 160,
         "height": 48},
        {"name": "content", "x": 96, "y": 120, "width": 600,
         "height": 200}]}
    assert "R002" not in _rule_ids(visual.judge(site, layout=off_centre))


def test_r005_shared_axes_off_grid_offset(tmp_path: Path):
    violating = BASE_CSS + ".card { margin-left: 3px; }\n"
    finding = _finding(visual.judge(_site(tmp_path, css=violating)), "R005")
    assert finding is not None and "3px" in finding.actual
    clean = BASE_CSS + ".card { margin-left: 8px; }\n"
    assert "R005" not in _rule_ids(visual.judge(_site(tmp_path, css=clean)))


def test_r005_layout_boxes_off_grid_edge(tmp_path: Path):
    site = _site(tmp_path)
    bad_layout = {"1440": [
        {"name": "content", "x": 0, "y": 0, "width": 800, "height": 600},
        {"name": "sidebar", "x": 3, "y": 0, "width": 500, "height": 600}]}
    assert "R005" in _rule_ids(visual.judge(site, layout=bad_layout))
    good_layout = {"1440": [
        {"name": "content", "x": 0, "y": 0, "width": 800, "height": 600},
        {"name": "sidebar", "x": 8, "y": 0, "width": 500, "height": 600}]}
    assert "R005" not in _rule_ids(visual.judge(site, layout=good_layout))


def _red_share_shot(tmp_path: Path, red_fraction: float) -> str:
    from PIL import Image, ImageDraw

    shot = tmp_path / "shot-1440.png"
    img = Image.new("RGB", (200, 100), (255, 255, 255))
    height = round(100 * red_fraction)
    ImageDraw.Draw(img).rectangle([0, 0, 199, height - 1],
                                  fill=(220, 38, 38))
    img.save(shot)
    return str(shot)


def test_r019_rendered_pixel_shares(tmp_path: Path):
    site = _site(tmp_path, css=R019_CSS)
    small_accent = visual.judge(site, screenshots=[_red_share_shot(tmp_path, 0.04)])
    assert small_accent.measured["r019_basis"] == "rendered"
    assert small_accent.measured["r019_shares"]["accent"] == 0.04
    assert "R019" not in _rule_ids(small_accent)
    big_accent = visual.judge(site, screenshots=[_red_share_shot(tmp_path, 0.40)])
    assert big_accent.measured["r019_basis"] == "rendered"
    finding = _finding(big_accent, "R019")
    assert finding is not None and "40%" in finding.actual


def test_r019_rendered_saturated_dominant(tmp_path: Path):
    from PIL import Image

    css = ("body { font-size: 16px; line-height: 1.5; color: #ffffff; "
           "background-color: #2563eb; }\n"
           ".hero { background-color: #2563eb; }\n"
           ".card { background-color: #2563eb; }\n"
           ".btn-primary { background-color: #dc2626; color: #ffffff; }\n")
    shot = tmp_path / "shot-1440.png"
    Image.new("RGB", (200, 100), (37, 99, 235)).save(shot)
    verdict = visual.judge(_site(tmp_path, css=css), screenshots=[str(shot)])
    assert verdict.measured["r019_basis"] == "rendered"
    finding = _finding(verdict, "R019")
    assert finding is not None and "saturated" in finding.actual


def test_r019_without_screenshots_is_a_labelled_estimate(tmp_path: Path):
    css = ("body { color: #111827; background-color: #ffffff; }\n"
           "a { color: #2563eb; }\n.one { color: #dc2626; }\n"
           ".two { color: #15803d; }\n.three { color: #d97706; }\n"
           ".four { color: #7c3aed; }\n")
    verdict = visual.judge(_site(tmp_path, css=css))
    assert verdict.measured["r019_basis"] == "estimated"
    finding = _finding(verdict, "R019")
    assert finding is not None
    assert "estimate from declarations" in finding.actual
    assert "not a rendered measurement" in finding.actual


def test_r037_z_pattern_logo_position(tmp_path: Path):
    violating_html = BASE_HTML.replace(
        '<header><div class="logo">Acme</div><nav>',
        '<header><nav><a href="index.html">Home</a></nav>'
        '<div class="logo">Acme</div><nav>')
    assert "R037" in _rule_ids(visual.judge(_site(tmp_path, html=violating_html)))
    inverted_html = BASE_HTML.replace(
        '<header><div class="logo">Acme</div>',
        '<header><a class="btn-primary" href="contact.html">Start</a>'
        '<div class="logo">Acme</div>')
    assert "R037" in _rule_ids(visual.judge(_site(tmp_path, html=inverted_html)))
    assert "R037" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r037_layout_boxes(tmp_path: Path):
    site = _site(tmp_path)
    bad_layout = {"1440": [
        {"name": "logo", "x": 1200, "y": 700, "width": 120, "height": 40},
        {"name": "hero", "x": 0, "y": 0, "width": 1440, "height": 800},
        {"name": "primary_cta", "x": 24, "y": 16, "width": 160,
         "height": 48}]}
    assert "R037" in _rule_ids(visual.judge(site, layout=bad_layout))
    good_layout = {"1440": [
        {"name": "logo", "x": 24, "y": 16, "width": 120, "height": 40},
        {"name": "hero", "x": 0, "y": 0, "width": 1440, "height": 800},
        {"name": "primary_cta", "x": 1200, "y": 700, "width": 160,
         "height": 48}]}
    assert "R037" not in _rule_ids(visual.judge(site, layout=good_layout))


def test_r062_proximity_label_farther_from_own_field(tmp_path: Path):
    violating = BASE_CSS.replace(
        "label { display: block; margin-bottom: 4px; }",
        "label { display: block; margin-bottom: 24px; }").replace(
        "input, textarea, select { min-height: 48px; padding: 8px 16px; "
        "margin-bottom: 24px; }",
        "input, textarea, select { min-height: 48px; padding: 8px 16px; "
        "margin-bottom: 4px; }")
    finding = _finding(visual.judge(_site(tmp_path, css=violating)), "R062")
    assert finding is not None and "24px" in finding.actual
    assert "R062" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r063_similarity_links_and_buttons(tmp_path: Path):
    links = (BASE_CSS + "nav a { color: #dc2626; }\n"
             "footer a { color: #15803d; text-decoration: underline; }\n")
    assert "R063" in _rule_ids(visual.judge(_site(tmp_path, css=links)))
    assert "R063" not in _rule_ids(visual.judge(_site(tmp_path)))
    buttons_css = BASE_CSS + (".cta-primary { background-color: #dc2626; "
                              "color: #ffffff; }\n")
    buttons_html = BASE_HTML.replace(
        '<a class="btn-primary" href="contact.html">Start a project</a>',
        '<a class="btn-primary" href="contact.html">Start a project</a>'
        '<a class="cta-primary" href="contact.html">Start now</a>')
    assert "R063" in _rule_ids(visual.judge(
        _site(tmp_path, css=buttons_css, html=buttons_html)))


def test_r064_common_region_plan_without_card(tmp_path: Path):
    pricing = ('<div class="price">$19/mo</div>'
               '<ul class="features"><li>Ten pages</li></ul>'
               '<a class="btn-primary" href="#">Choose</a>')
    floating = BASE_HTML.replace("</main>", pricing + "</main>")
    assert "R064" in _rule_ids(visual.judge(_site(tmp_path, html=floating)))
    card = ('<article class="plan-card"><div class="price">$19/mo</div>'
            '<ul class="features"><li>Ten pages</li></ul>'
            '<a class="btn-primary" href="#">Choose</a></article>')
    grouped = BASE_HTML.replace("</main>", card + "</main>")
    assert "R064" not in _rule_ids(visual.judge(_site(tmp_path, html=grouped)))


def test_r065_closure_carousel_without_peek(tmp_path: Path):
    carousel_html = BASE_HTML.replace(
        "</main>",
        '<div class="carousel"><div class="carousel-item">One</div>'
        '<div class="carousel-item">Two</div></div></main>')
    hidden = BASE_CSS + (".carousel { overflow: hidden; }\n"
                         ".carousel-item { width: 100%; }\n")
    assert "R065" in _rule_ids(visual.judge(
        _site(tmp_path, css=hidden, html=carousel_html)))
    peek = BASE_CSS + (".carousel { overflow: hidden; }\n"
                       ".carousel-item { width: 85%; }\n")
    assert "R065" not in _rule_ids(visual.judge(
        _site(tmp_path, css=peek, html=carousel_html)))


def test_r066_continuity_scattered_steps(tmp_path: Path):
    steps_html = BASE_HTML.replace(
        "</main>",
        '<ol class="steps"><li class="step step-a">One</li>'
        '<li class="step step-b">Two</li>'
        '<li class="step step-c">Three</li></ol></main>')
    scattered = BASE_CSS.replace(".steps { display: flex; }",
                                 ".steps { display: block; }") + (
        ".step-a { margin-left: 0; }\n.step-b { margin-left: 37px; }\n"
        ".step-c { margin-left: 12px; }\n")
    finding = _finding(visual.judge(
        _site(tmp_path, css=scattered, html=steps_html)), "R066")
    assert finding is not None and "37px" in finding.actual
    assert "R066" not in _rule_ids(visual.judge(
        _site(tmp_path, html=steps_html)))


def test_r067_figure_ground_modal_without_overlay(tmp_path: Path):
    modal_html = BASE_HTML.replace(
        "</main>", '<div class="modal">Are you sure?</div></main>')
    assert "R067" in _rule_ids(visual.judge(_site(tmp_path, html=modal_html)))
    dimmed_html = BASE_HTML.replace(
        "</main>",
        '<div class="modal">Are you sure?</div>'
        '<div class="modal-overlay"></div></main>')
    assert "R067" not in _rule_ids(visual.judge(
        _site(tmp_path, html=dimmed_html)))


def test_r067_figure_ground_hero_cta_separation(tmp_path: Path):
    flat = BASE_CSS.replace(
        ".hero { padding: 96px 24px; background-color: #ffffff; }",
        ".hero { padding: 96px 24px; background-color: #2563eb; }")
    finding = _finding(visual.judge(_site(tmp_path, css=flat)), "R067")
    assert finding is not None and "1.0:1" in finding.actual
    assert "R067" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r068_common_fate_mismatched_pair(tmp_path: Path):
    mismatched = BASE_CSS.replace(
        ".modal-overlay { transition: opacity 200ms ease-out;",
        ".modal-overlay { transition: opacity 400ms ease-in;")
    finding = _finding(visual.judge(_site(tmp_path, css=mismatched)), "R068")
    assert finding is not None and "400ms" in finding.actual
    assert "R068" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r069_pragnanz_icon_only_button(tmp_path: Path):
    icon = ('<button type="button"><svg width="16" height="16">'
            '<path d="M0 0h16v16H0z"/></svg></button>')
    unlabelled = BASE_HTML.replace("</main>", icon + "</main>")
    assert "R069" in _rule_ids(visual.judge(_site(tmp_path, html=unlabelled)))
    labelled = BASE_HTML.replace(
        "</main>",
        icon.replace("<button", '<button aria-label="Search"') + "</main>")
    assert "R069" not in _rule_ids(visual.judge(_site(tmp_path, html=labelled)))


def test_r069_pragnanz_inconsistent_icon_strokes(tmp_path: Path):
    strokes = BASE_HTML.replace(
        "</main>",
        '<svg stroke-width="1"></svg><svg stroke-width="3"></svg></main>')
    finding = _finding(visual.judge(_site(tmp_path, html=strokes)), "R069")
    assert finding is not None and "stroke-width" in finding.actual


def test_r070_symmetry_stepper_without_connector(tmp_path: Path):
    stepper_html = BASE_HTML.replace(
        "</main>",
        '<ol class="stepper"><li class="step">One</li>'
        '<li class="step">Two</li></ol></main>')
    assert "R070" in _rule_ids(visual.judge(_site(tmp_path, html=stepper_html)))
    connected_html = BASE_HTML.replace(
        "</main>",
        '<ol class="stepper"><li class="step">One</li>'
        '<span class="step-connector"></span>'
        '<li class="step">Two</li></ol></main>')
    assert "R070" not in _rule_ids(visual.judge(
        _site(tmp_path, html=connected_html)))


def test_r070_symmetry_unequal_footer_columns(tmp_path: Path):
    unequal = BASE_CSS.replace("grid-template-columns: 1fr 1fr 1fr",
                               "grid-template-columns: 5fr 1fr 1fr")
    finding = _finding(visual.judge(_site(tmp_path, css=unequal)), "R070")
    assert finding is not None and "5fr" in finding.actual
    assert "R070" not in _rule_ids(visual.judge(_site(tmp_path)))


def test_r093_animation_needs_a_job(tmp_path: Path):
    decorative = BASE_CSS + ".card { animation: fadeUp 600ms ease both; }\n"
    assert "R093" in _rule_ids(visual.judge(_site(tmp_path, css=decorative)))
    triggered = BASE_CSS + (".card.is-visible { animation: fadeUp 300ms "
                            "ease both; }\n")
    assert "R093" not in _rule_ids(visual.judge(_site(tmp_path, css=triggered)))
