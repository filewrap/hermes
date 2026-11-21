"""Behaviour contracts for ``gateway/web_designer/judgers/`` and the tool
registry (``gateway/web_designer/tool_registry.py``).

A good site passes all five judgers; a bad site fails each one with the
exact violation (contrast failure, missing alt, lazy hero, unlabelled
input, raw hex drift). Live metrics are never invented: without them the
Core Web Vitals rules are unchecked and nothing is measured.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gateway.web_designer.judgers import JUDGERS, judge_all
from gateway.web_designer.judgers.common import (
    contrast_ratio, parse_color, parse_css, parse_html,
)
from gateway.web_designer.judgers import a11y, content, performance, responsive, visual
from gateway.web_designer.rules import rules_for_judger
from gateway.web_designer.tool_registry import (
    TOOLS, get_tool, live_checker_available, live_checkers, tools_by_bucket,
)

GOOD_CSS = """\
:root {
  --color-text-primary: #111827;
  --color-surface-base: #ffffff;
  --color-surface-muted: #f9fafb;
  --color-action-primary: #2563eb;
  --color-border-subtle: #e5e7eb;
  --color-success: #15803d;
  --color-error: #dc2626;
}
body { font-size: 16px; line-height: 1.5; color: #111827; background-color: #ffffff; margin: 0; font-family: Inter, system-ui; }
h1 { font-size: clamp(32px, 5vw, 40px); line-height: 1.1; letter-spacing: -0.02em; color: #111827; }
h2 { font-size: 25px; line-height: 1.2; color: #111827; }
h3 { font-size: 20px; line-height: 1.2; color: #111827; }
p { margin-bottom: 24px; max-width: 66ch; }
.content { max-width: 66ch; margin: 0 auto; }
.layout { display: grid; grid-template-columns: repeat(12, 1fr); gap: 24px; }
main { margin: 48px auto; }
section { padding: 48px 24px; margin: 48px 0; background-color: #f9fafb; }
.hero { padding: 96px 24px; background-color: #ffffff; min-height: 100svh; }
.card { display: grid; grid-template-rows: subgrid; grid-row: span 3; padding: 24px; background-color: #ffffff; border: 1px solid #e5e7eb; }
.btn-primary { display: inline-block; min-height: 48px; padding: 16px 24px; background-color: #2563eb; color: #ffffff; font-size: 16px; transition: transform 200ms ease-out, opacity 200ms ease-out; }
.btn-secondary { display: inline-block; min-height: 48px; padding: 16px 24px; background-color: #ffffff; color: #2563eb; border: 1px solid #2563eb; font-size: 16px; }
a { color: #2563eb; }
a:hover { color: #1d4ed8; }
a:focus-visible { outline: 3px solid #2563eb; outline-offset: 2px; }
button:focus-visible { outline: 3px solid #2563eb; outline-offset: 2px; }
nav a { min-height: 44px; display: inline-block; padding: 8px 16px; }
footer { background-color: #111827; color: #ffffff; padding: 48px 24px; }
.success { color: #15803d; border-left: 4px solid #15803d; font-weight: 600; }
.error { color: #dc2626; border-left: 4px solid #dc2626; font-weight: 600; }
input, textarea, select { min-height: 48px; padding: 8px 16px; border: 1px solid #e5e7eb; }
label { display: block; margin-bottom: 8px; }
form { margin: 24px 0; }
.modal { transition: opacity 200ms ease-out; }
.modal-overlay { transition: opacity 200ms ease-out; }
@media (min-width: 700px) { .layout { gap: 24px; } .card { padding: 24px; } }
@media (min-width: 1100px) { .content { max-width: 66ch; } }
@media (max-width: 700px) { .btn-primary { position: fixed; bottom: calc(16px + env(safe-area-inset-bottom)); } }
@media (prefers-reduced-motion: reduce) { * { animation-duration: 0.01ms; transition-duration: 0.01ms; } }
@media (prefers-color-scheme: dark) { body { background-color: #121212; color: #e0e0e0; } }
"""

GOOD_JS = """\
document.querySelectorAll('input, textarea').forEach(function (field) {
  field.addEventListener('blur', function () {
    if (!field.value) { field.setCustomValidity('Please fill in this field before sending.'); }
    else { field.setCustomValidity(''); }
  });
});
document.addEventListener('keydown', function (event) {
  if (event.key === 'Escape') { var d = document.getElementById('thanks'); if (d) d.close(); }
});
"""


def _good_head(title: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{title}</title><link rel="stylesheet" href="styles.css"></head>
<body><a class="skip-link" href="#main">Skip to content</a>
<header><nav aria-label="Primary"><a href="index.html">Home</a><a href="about.html">About</a><a href="#services">Services</a><a href="#work">Work</a><a href="#blog">Blog</a><a href="contact.html">Contact</a></nav></header>
<main id="main">
"""


_GOOD_FOOT = """</main><footer><p>Good Studio makes clear websites for small teams.</p></footer>
<script src="app.js"></script></body></html>"""

GOOD_INDEX = _good_head("Good Studio — clear websites") + """
<section class="hero"><h1>Clear websites for small teams</h1><p>We design fast, accessible websites that explain your work in plain words.</p><a class="btn-primary" href="contact.html">Start a project</a><a class="btn-secondary" href="#work">See our work</a></section>
<section id="services"><h2>What we do</h2><p>We plan, design, and build websites with your team.</p><ul><li>Design direction</li><li>Accessible builds</li><li>Honest pricing</li></ul>
<div class="layout"><article class="card"><h3>Design</h3><p>Simple layouts with clear hierarchy.</p></article><article class="card"><h3>Build</h3><p>Fast pages with clean code.</p></article></div></section>
<section id="work"><h2>Recent work</h2><picture><source type="image/webp" srcset="hero.webp"><img class="hero-img" src="hero.webp" srcset="hero.webp 800w" sizes="(max-width: 700px) 100vw, 800px" width="800" height="450" fetchpriority="high" alt="A bright studio homepage on a laptop screen"></picture>
<img src="team.webp" srcset="team.webp 600w" sizes="600px" width="600" height="400" loading="lazy" alt="Three designers reviewing a layout together"></section>
""" + _GOOD_FOOT

GOOD_ABOUT = _good_head("About — Good Studio") + """
<nav aria-label="breadcrumb"><a href="index.html">Home</a> / About</nav>
<h1>About Good Studio</h1><p>We are a small studio with ten years of practice.</p><a class="btn-primary" href="contact.html">Work with us</a>
""" + _GOOD_FOOT

GOOD_CONTACT = _good_head("Contact — Good Studio") + """
<nav aria-label="breadcrumb"><a href="index.html">Home</a> / Contact</nav>
<h1>Contact Good Studio</h1><p>Tell us about your project and we will reply within two days.</p>
<form action="contact.html" method="post">
<label for="name">Your name</label><input id="name" name="name" type="text" autocomplete="name">
<label for="email">Email address</label><input id="email" name="email" type="email" autocomplete="email">
<label for="phone">Phone number</label><input id="phone" name="phone" type="tel" autocomplete="tel">
<label for="company">Company</label><input id="company" name="company" type="text" autocomplete="organization">
<label for="budget">Budget</label><select id="budget" name="budget"><option>Under 5k</option><option>5k to 20k</option></select>
<label for="message">Project details</label><textarea id="message" name="message"></textarea>
<button class="btn-primary" type="submit">Send message</button></form>
<dialog id="thanks"><p>Thanks for writing to us.</p></dialog>
""" + _GOOD_FOOT

BAD_CSS = """\
body { font-size: 14px; line-height: 1.3; color: #777777; background-color: #ffffff; margin: 0; font-family: Inter, Georgia, Roboto, monospace; }
h1 { font-size: 20px; line-height: 1.6; color: #777777; }
h2 { font-size: 28px; line-height: 1.6; }
h3 { font-size: 24px; line-height: 1.6; }
p { margin-bottom: 13px; text-align: justify; }
.layout { display: flex; flex-direction: row; gap: 16px; }
main { margin: 7px; }
section { padding: 13px; margin: 16px; }
.card { padding: 48px; background-color: #ff5733; }
.btn-primary { background-color: #ff5733; color: #ffffff; min-height: 20px; transition: width 500ms ease; }
.btn-secondary { background-color: #ff5733; color: #ffffff; }
a { color: #777777; }
input { min-height: 20px; }
form { display: grid; grid-template-columns: 1fr 1fr; }
.hero { padding: 8px; }
.success { color: #dc2626; }
.error { color: #15803d; }
:focus { outline: none; }
@media (max-width: 768px) { .layout { flex-direction: column; } }
@media (max-width: 1024px) { body { font-size: 13px; } }
"""

BAD_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Bad Site</title><link rel="stylesheet" href="styles.css"></head>
<body>
<header><nav><a href="/one">One</a><a href="/two">Two</a><a href="/three">Three</a></nav></header>
<main>
<section><h2>First section with no headline first</h2><p>This is a very long paragraph that keeps going and going with many words and no structure at all, and it never stops, and it has far more than twenty words in a single sentence which is hard to read for anyone, and it just keeps going on and on without any break or pause or mercy for the reader at all.</p></section>
<section><h1>Buried headline</h1><p>Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. [verify] our claim about being the best.</p><a class="btn-primary" href="/buy">Buy now</a><a class="btn-primary" href="/buy2">Buy again</a></section>
<img src="hero.png" loading="lazy" alt="">
<img src="photo.png">
<img src="missing.png" alt="photo">
<form action="/submit" method="post">
<input name="email" type="text" placeholder="Email address">
<input name="phone" type="text" placeholder="Phone">
<input name="f3" type="text" placeholder="Field 3"><input name="f4" type="text" placeholder="Field 4">
<input name="f5" type="text" placeholder="Field 5"><input name="f6" type="text" placeholder="Field 6">
<input name="f7" type="text" placeholder="Field 7"><input name="f8" type="text" placeholder="Field 8">
<input name="f9" type="text" placeholder="Field 9"><input name="f10" type="text" placeholder="Field 10">
<button type="submit">Send</button></form>
<a href="/does-not-exist">Broken link</a>
<a href="https://example.com/external">External link</a>
<div onclick="go()">Click me</div>
</main><footer><p>Bad site footer.</p></footer>
<script src="app.js"></script></body></html>"""

BAD_TOKENS = {
    "primitive": {
        "color": {
            "brand": {"value": "#2563eb"},
            "ink": {"value": "#111827"},
        },
        "spacing": {"md": {"value": "16px"}},
    },
    "semantic": {"color": {"text": {"primary": {"value": "{primitive.color.ink}"}}}},
    "component": {},
}


@pytest.fixture
def good_site(tmp_path: Path) -> Path:
    (tmp_path / "styles.css").write_text(GOOD_CSS, encoding="utf-8")
    (tmp_path / "index.html").write_text(GOOD_INDEX, encoding="utf-8")
    (tmp_path / "about.html").write_text(GOOD_ABOUT, encoding="utf-8")
    (tmp_path / "contact.html").write_text(GOOD_CONTACT, encoding="utf-8")
    (tmp_path / "app.js").write_text(GOOD_JS, encoding="utf-8")
    (tmp_path / "budgets.json").write_text(
        '{"budgets": [{"resource": "script", "budget": 200}]}',
        encoding="utf-8")
    (tmp_path / "hero.webp").write_bytes(b"RIFFxxxxWEBP")
    (tmp_path / "team.webp").write_bytes(b"RIFFxxxxWEBP")
    return tmp_path


@pytest.fixture
def bad_site(tmp_path: Path) -> Path:
    (tmp_path / "styles.css").write_text(BAD_CSS, encoding="utf-8")
    (tmp_path / "index.html").write_text(BAD_HTML, encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "// " + "x" * 210_000, encoding="utf-8")
    (tmp_path / "hero.png").write_bytes(b"\x89PNG" + b"x" * 100)
    (tmp_path / "photo.png").write_bytes(b"\x89PNG" + b"x" * 100)
    return tmp_path


def _rule_ids(verdict) -> set[str]:
    return {f.rule_id for f in verdict.findings}


def test_judge_all_runs_five_judgers_in_stage_order(good_site: Path):
    verdicts = judge_all(good_site)
    assert tuple(verdicts) == JUDGERS == (
        "visual", "a11y", "performance", "content", "responsive")
    assert all(v.judger == name for name, v in verdicts.items())


def test_unjudged_when_no_project_files(tmp_path: Path):
    assert visual.judge(tmp_path / "missing").unjudged
    assert not visual.judge(tmp_path / "missing").passed
    assert a11y.judge(tmp_path).unjudged  # empty dir
    assert judge_all(tmp_path)["content"].unjudged


def test_good_site_passes_all_judgers(good_site: Path):
    verdicts = judge_all(good_site)
    assert all(v.passed for v in verdicts.values())
    assert all(not v.unjudged for v in verdicts.values())
    for verdict in verdicts.values():
        assert not [f for f in verdict.findings
                    if f.severity in {"blocker", "defect"}]


def test_good_site_unchecked_without_screenshots(good_site: Path):
    verdicts = judge_all(good_site)
    assert verdicts["visual"].unchecked == []
    assert verdicts["a11y"].unchecked == []
    assert verdicts["performance"].unchecked == ["R045", "R083", "R084", "R085"]
    assert verdicts["content"].unchecked == []
    assert verdicts["responsive"].unchecked == ["R096"]


def test_screenshots_clear_screenshot_dependent_rules(
        good_site: Path, tmp_path: Path):
    shots = []
    for width in (1440, 768, 375):
        shot = tmp_path / f"{width}.png"
        shot.write_bytes(b"fake-png")
        shots.append(str(shot))
    verdicts = judge_all(good_site, screenshots=shots)
    assert verdicts["visual"].unchecked == []
    assert verdicts["responsive"].unchecked == []
    assert verdicts["visual"].passed and verdicts["responsive"].passed


def test_bad_site_fails_every_judger(bad_site: Path):
    verdicts = judge_all(bad_site)
    assert all(not v.passed for v in verdicts.values())


def test_bad_site_exact_violations(bad_site: Path):
    verdicts = judge_all(bad_site, tokens=BAD_TOKENS)
    a11y_ids = _rule_ids(verdicts["a11y"])
    assert "R072" in a11y_ids  # #777777 on white is below 4.5:1
    assert "R078" in a11y_ids  # img with no alt / empty alt
    assert "R076" in a11y_ids  # bare outline:none
    assert "R077" in a11y_ids  # div with onclick
    assert "R088" in _rule_ids(verdicts["performance"])  # lazy hero
    assert "R101" in _rule_ids(verdicts["content"])  # placeholder as label
    assert "R009" in _rule_ids(verdicts["visual"])  # body below 16px
    drift = [f for f in verdicts["visual"].findings
             if f.rule_id == "R027" and "#ff5733" in f.actual]
    assert drift and drift[0].severity == "defect"  # raw hex, twice


def test_live_metrics_are_measured_never_invented(good_site: Path):
    without = performance.judge(good_site)
    assert without.unchecked == ["R045", "R083", "R084", "R085"]
    assert "lcp" not in without.measured and "inp" not in without.measured
    assert "cls" not in without.measured

    passing = performance.judge(
        good_site, live_metrics={"lcp": 1.8, "inp": 120, "cls": 0.05})
    assert passing.passed and passing.unchecked == ["R045"]
    assert passing.measured["lcp"] == 1.8
    assert passing.measured["inp"] == 120
    assert passing.measured["cls"] == 0.05

    failing = performance.judge(good_site, live_metrics={"lcp": 4.0})
    assert not failing.passed
    assert "R083" in _rule_ids(failing)
    assert failing.unchecked == ["R045", "R084", "R085"]  # unmeasured stay unchecked


def test_checked_plus_unchecked_covers_every_rule(good_site: Path):
    verdicts = judge_all(good_site)
    for name, verdict in verdicts.items():
        owned = {r["id"] for r in rules_for_judger(name)}
        covered = set(verdict.measured["checked_rules"]) | set(verdict.unchecked)
        assert covered == owned, name


def test_verify_flag_blocks_delivery(good_site: Path):
    verdict = content.judge(
        good_site, content_draft="Our uptime is 99.99% [verify] before launch.")
    blockers = [f for f in verdict.findings if f.severity == "blocker"]
    assert blockers and all(f.rule_id == "R108" for f in blockers)
    assert not verdict.passed


def test_broken_relative_link_is_a_defect_external_link_is_unchecked(
        bad_site: Path):
    verdict = content.judge(bad_site)
    broken = [f for f in verdict.findings if f.rule_id == "R107"]
    assert broken and any("/does-not-exist" in f.actual for f in broken)
    assert "https://example.com/external" in (
        verdict.measured["external_links_unchecked"])
    assert not [f for f in verdict.findings
                if "example.com" in f.actual]


def test_contrast_math_is_real_wcag():
    assert contrast_ratio(parse_color("#000000"), parse_color("#ffffff")) == 21.0
    assert contrast_ratio(parse_color("#777777"), parse_color("#ffffff")) < 4.5
    assert contrast_ratio(parse_color("#111827"), parse_color("#ffffff")) > 4.5
    assert parse_color("rgb(255, 0, 0)") == (255, 0, 0)
    assert parse_color("hsl(0, 100%, 50%)") == (255, 0, 0)
    assert parse_color("not-a-colour") is None


def test_css_and_html_parsers():
    rules = parse_css(
        "body { color: #111827; }\n"
        "@media (min-width: 700px) { p { margin: 8px; } }")
    assert rules[0].media is None
    assert next(r for r in rules if r.media).media == "@media (min-width: 700px)"
    doc = parse_html('<a class="skip-link" href="#main">Skip</a>'
                     '<img src="x.png" alt="A photo">')
    assert doc.find("a")[0].attrs["class"] == "skip-link"
    assert doc.find("img")[0].attrs["alt"] == "A photo"


def test_tool_registry_has_64_tools_in_14_buckets():
    assert len(TOOLS) == 64
    assert len({t["bucket"] for t in TOOLS}) == 14
    for tool in TOOLS:
        assert {"name", "url", "bucket", "license", "kind",
                "what", "hermes_use"} <= set(tool)
        assert tool["url"].startswith("https://")
        assert tool["kind"] in {"live", "reference"}
        assert tool["license"]
        assert tool["what"] and tool["hermes_use"]
    assert len({t["name"] for t in TOOLS}) == 64
    for bucket in {t["bucket"] for t in TOOLS}:
        assert tools_by_bucket(bucket)


def test_live_checkers_are_the_nine_live_slots():
    names = {t["name"] for t in live_checkers()}
    assert names == {"W3C Markup Validation Service",
                     "W3C CSS Validation Service",
                     "Dead Link Checker", "Lighthouse", "axe DevTools",
                     "WebAIM Contrast Checker", "BackstopJS",
                     "Google Fonts", "Fontshare"}
    assert live_checker_available("WebAIM Contrast Checker")
    assert live_checker_available("W3C Markup Validation Service")
    assert live_checker_available("W3C CSS Validation Service")
    assert live_checker_available("Dead Link Checker")
    assert live_checker_available("BackstopJS")
    assert live_checker_available("Google Fonts")
    assert live_checker_available("Fontshare")
    assert live_checker_available("axe DevTools") == (
        shutil.which("axe") is not None)
    assert live_checker_available("Lighthouse") == (
        shutil.which("lighthouse") is not None)
    assert not live_checker_available("Coolors")  # reference-only tool


def test_judge_all_accepts_layout_and_axe_results(good_site: Path):
    # Every judger accepts the same optional inputs, so the coordinator
    # passes layout boxes and axe output through judge_all without a
    # per-judger filter; judgers that do not use an input ignore it.
    layout = {"1440": [{"name": "hero", "x": 0, "y": 0, "width": 1440,
                        "height": 600}]}
    verdicts = judge_all(good_site, layout=layout,
                         axe_results={"violations": []})
    assert verdicts["visual"].measured["layout_basis"] == "layout"
    assert verdicts["a11y"].measured["axe_violations"] == 0
    assert verdicts["visual"].passed and verdicts["a11y"].passed


def test_live_checkers_wire_into_visual_and_a11y(tmp_path: Path):
    # The registry's live checkers run inside the judgers: font sources
    # and icon consistency become R013/R069 findings, CSS validity and
    # HTML parse errors land in measured (no audit rule maps to them).
    (tmp_path / "index.html").write_text(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>Checker Site</title>"
        "<link rel=\"stylesheet\" href=\"styles.css\"></head><body>"
        "<button><svg width=\"24\" height=\"24\" stroke-width=\"2\">"
        "<path d=\"M0 0h24v24H0z\"/></svg></button>"
        "<div><p>Unclosed paragraph.</body></html>", encoding="utf-8")
    (tmp_path / "styles.css").write_text(
        "@font-face { font-family: 'Nope'; "
        "src: url('https://evil-fonts.example/nope.woff2'); }\n"
        "body { colour: #111827; font-size: 16px; }\n",
        encoding="utf-8")
    visual_verdict = visual.judge(tmp_path)
    assert "R013" in _rule_ids(visual_verdict)  # unlicensed font source
    r069 = [f for f in visual_verdict.findings if f.rule_id == "R069"]
    assert len(r069) == 1  # icon button flagged once, not twice
    assert any("colour" in e
               for e in visual_verdict.measured["css_validation_errors"])
    a11y_verdict = a11y.judge(tmp_path)
    assert a11y_verdict.measured["html_parse_errors"] > 0


def test_tailwind_shadcn_daisyui_overlap_kept_by_design():
    for name in ("Tailwind CSS", "shadcn/ui", "DaisyUI"):
        tool = get_tool(name)
        assert tool is not None
        assert tool["bucket"] == "templates/components"
    assert get_tool("No Such Tool") is None
