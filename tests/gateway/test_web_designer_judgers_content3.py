"""Behaviour contracts for the content judger's UX/psychology rules
(R041-R061, R106, R110).

Each rule gets a violating fixture (the rule fires with its own id) and a
clean fixture (the rule stays silent). Fixtures are minimal tmp_path
projects; assertions are on rule ids in the verdict, never on the verdict
passing as a whole, so unrelated rules firing does not couple the tests.
"""
from __future__ import annotations

from pathlib import Path

from gateway.web_designer.judgers import content
from gateway.web_designer.rules import rules_for_judger


def _page(body: str) -> str:
    # Body on its own lines: the judger reads element text and line ranges,
    # so the title text must not share a line with body content.
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n<title>T</title>\n</head>\n<body>\n"
            f"{body}\n</body>\n</html>")


def _site(tmp_path: Path, pages: dict[str, str]) -> Path:
    for rel, html in pages.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
    return tmp_path


def _write_js(tmp_path: Path, js: str) -> None:
    (tmp_path / "app.js").write_text(js, encoding="utf-8")


def _ids(verdict) -> set[str]:
    return {f.rule_id for f in verdict.findings}


def _nav_links(n: int) -> str:
    items = ['<a href="index.html">Home</a>']
    items += [f'<a href="p{i}.html">Page {i}</a>' for i in range(1, n)]
    return "<nav>\n" + "\n".join(items) + "\n</nav>"


def _form(n_fields: int, action: str | None = "/submit",
          extra: str = "") -> str:
    fields = "".join(
        f'<label for="f{i}">Field {i}</label>'
        f'<input id="f{i}" name="f{i}" type="text">'
        for i in range(n_fields))
    action_attr = f' action="{action}"' if action else ""
    return (f"<form{action_attr} method=\"post\">{fields}{extra}"
            "<button type=\"submit\">Send message</button></form>")


def _select(n_options: int, grouped: bool = False) -> str:
    options = "".join(f"<option>Choice {i}</option>" for i in range(n_options))
    if grouped:
        half = n_options // 2
        options = ("<optgroup label=\"A\">"
                   + "".join(f"<option>Choice {i}</option>"
                             for i in range(half))
                   + "</optgroup><optgroup label=\"B\">"
                   + "".join(f"<option>Choice {i}</option>"
                             for i in range(half, n_options))
                   + "</optgroup>")
    return f"<label for=\"pick\">Pick one</label><select id=\"pick\">{options}</select>"


def _plan_cards(names, marked: str | None = None,
                marker: str = "Most popular") -> str:
    cards = []
    for name in names:
        tag = f"<p>{marker}</p>" if name == marked else ""
        cards.append(f'<article class="plan-card"><h3>{name}</h3>{tag}'
                     "<p>$9 a month</p></article>")
    return "\n".join(cards)


# --- R041 Hick's Law ----------------------------------------------------------

def test_r041_nine_ungrouped_nav_links_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_nav_links(9))})
    assert "R041" in _ids(content.judge(site))


def test_r041_six_nav_links_pass(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_nav_links(6))})
    assert "R041" not in _ids(content.judge(site))


def test_r041_select_with_nine_options_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_select(9))})
    assert "R041" in _ids(content.judge(site))


def test_r041_select_with_optgroups_passes(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_select(9, grouped=True))})
    assert "R041" not in _ids(content.judge(site))


# --- R042 Jakob's Law ---------------------------------------------------------

def test_r042_header_first_link_not_home_flagged(tmp_path: Path):
    body = ('<header>\n<a href="about.html">About</a>\n'
            '<a href="index.html">Home</a>\n</header>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R042" in _ids(content.judge(site))


def test_r042_header_first_link_home_passes(tmp_path: Path):
    body = ('<header>\n<a href="index.html">Home</a>\n'
            '<a href="about.html">About</a>\n</header>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R042" not in _ids(content.judge(site))


def test_r042_multi_page_site_without_nav_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<h1>One</h1>"),
        "about.html": _page("<h1>Two</h1>"),
        "contact.html": _page("<h1>Three</h1>"),
    })
    assert "R042" in _ids(content.judge(site))


def test_r042_search_outside_header_flagged(tmp_path: Path):
    body = ('<header>\n<a href="index.html">Home</a>\n</header>\n'
            '<main>\n<input type="search" name="q">\n</main>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R042" in _ids(content.judge(site))


# --- R043 Miller / chunking ---------------------------------------------------

def test_r043_unchunked_digit_string_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<p>Your reference is 12345678.</p>")})
    assert "R043" in _ids(content.judge(site))


def test_r043_chunked_digit_string_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<p>Your reference is 1234 5678.</p>")})
    assert "R043" not in _ids(content.judge(site))


def test_r043_form_section_above_seven_without_fieldset_flagged(
        tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_form(9))})
    assert "R043" in _ids(content.judge(site))


# --- R044 Tesler's Law ---------------------------------------------------------

def test_r044_identity_field_without_autocomplete_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="email">Email</label>'
            '<input id="email" name="email" type="email">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R044" in _ids(content.judge(site))


def test_r044_identity_fields_with_autocomplete_pass(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="email">Email</label>'
            '<input id="email" name="email" type="email" '
            'autocomplete="email">'
            '<label for="phone">Phone</label>'
            '<input id="phone" name="phone" type="tel" autocomplete="tel">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R044" not in _ids(content.judge(site))


def test_r044_exact_format_phone_pattern_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="phone">Phone</label>'
            '<input id="phone" name="phone" type="tel" autocomplete="tel" '
            'pattern="[0-9]{10}">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R044" in _ids(content.judge(site))


# --- R046 Peak-End ------------------------------------------------------------

def test_r046_form_without_success_state_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_form(2))})
    assert "R046" in _ids(content.judge(site))


def test_r046_form_with_thank_you_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(_form(2) + "<p>Thanks for writing to us.</p>")})
    assert "R046" not in _ids(content.judge(site))


# --- R047 Aesthetic-Usability -------------------------------------------------

def test_r047_dead_primary_cta_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page('<a class="btn-primary" href="#">Buy now</a>')})
    assert "R047" in _ids(content.judge(site))


def test_r047_primary_cta_with_real_destination_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(
            '<a class="btn-primary" href="/checkout">Buy now</a>')})
    assert "R047" not in _ids(content.judge(site))


def test_r047_submit_without_action_or_handler_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_form(2, action=None))})
    assert "R047" in _ids(content.judge(site))


# --- R048 Postel's Law --------------------------------------------------------

def test_r048_phone_pattern_forbidding_spaces_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="phone">Phone</label>'
            '<input id="phone" name="phone" type="tel" autocomplete="tel" '
            'pattern="[0-9]{10}">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R048" in _ids(content.judge(site))


def test_r048_phone_pattern_allowing_spaces_passes(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="phone">Phone</label>'
            '<input id="phone" name="phone" type="tel" autocomplete="tel" '
            'pattern="[0-9 -]{7,15}">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R048" not in _ids(content.judge(site))


def test_r048_exact_maxlength_on_free_text_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="message">Message</label>'
            '<input id="message" name="message" type="text" maxlength="10">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R048" in _ids(content.judge(site))


# --- R049 Don't Make Me Think -------------------------------------------------

def test_r049_generic_button_label_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page("<button>Submit</button>")})
    assert "R049" in _ids(content.judge(site))


def test_r049_generic_link_label_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page('<a href="/guide">Learn more</a>')})
    assert "R049" in _ids(content.judge(site))


def test_r049_specific_label_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<button>Send message</button>")})
    assert "R049" not in _ids(content.judge(site))


# --- R050 Progressive Disclosure ----------------------------------------------

def test_r050_ten_field_form_without_disclosure_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_form(10))})
    assert "R050" in _ids(content.judge(site))


def test_r050_ten_field_form_with_details_passes(tmp_path: Path):
    body = _form(10) + ("<details><summary>Advanced</summary>"
                        "<p>Extra options live here.</p></details>")
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R050" not in _ids(content.judge(site))


def test_r050_api_key_field_without_disclosure_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="api-key">API key</label>'
            '<input id="api-key" name="api-key" type="text">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R050" in _ids(content.judge(site))


# --- R051 Recognition over recall ----------------------------------------------

def test_r051_promo_field_without_helper_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="promo">Promo code</label>'
            '<input id="promo" name="promo-code" type="text">'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R051" in _ids(content.judge(site))


def test_r051_promo_field_with_helper_passes(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="promo">Promo code</label>'
            '<input id="promo" name="promo-code" type="text" '
            'aria-describedby="promo-help">'
            '<p id="promo-help">Find your code in your welcome email.</p>'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R051" not in _ids(content.judge(site))


def test_r051_copy_demanding_code_from_email_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(
            "<p>Enter the code from your email to continue.</p>")})
    assert "R051" in _ids(content.judge(site))


# --- R052 Undo -----------------------------------------------------------------

def test_r052_delete_without_undo_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<button>Delete account</button>")})
    assert "R052" in _ids(content.judge(site))


def test_r052_delete_with_undo_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<button>Delete account</button>"
                            "<button>Undo</button>")})
    assert "R052" not in _ids(content.judge(site))


# --- R053 Consistency -----------------------------------------------------------

def test_r053_submit_labels_differ_across_pages_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(
            '<form action="/a"><button type="submit">Save</button></form>'),
        "about.html": _page(
            '<form action="/b"><button type="submit">OK</button></form>'),
    })
    assert "R053" in _ids(content.judge(site))


def test_r053_same_submit_label_across_pages_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(
            '<form action="/a"><button type="submit">Send message</button>'
            "</form>"),
        "about.html": _page(
            '<form action="/b"><button type="submit">Send message</button>'
            "</form>"),
    })
    assert "R053" not in _ids(content.judge(site))


def test_r053_primary_button_classes_differ_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(
            '<a class="btn-primary" href="/a">Start</a>'),
        "about.html": _page(
            '<a class="cta-primary" href="/b">Start</a>'),
    })
    assert "R053" in _ids(content.judge(site))


# --- R054 System status ---------------------------------------------------------

def test_r054_form_with_no_feedback_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_form(2))})
    assert "R054" in _ids(content.judge(site))


def test_r054_form_with_live_region_passes(tmp_path: Path):
    body = _form(2) + '<div aria-live="polite"></div>'
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R054" not in _ids(content.judge(site))


# --- R055 Match the real world --------------------------------------------------

def test_r055_jargon_nav_label_flagged(tmp_path: Path):
    body = ("<nav>\n<a href=\"index.html\">Home</a>\n"
            "<a href=\"/admin\">Backend</a>\n</nav>")
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R055" in _ids(content.judge(site))


def test_r055_plain_nav_labels_pass(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_nav_links(3))})
    assert "R055" not in _ids(content.judge(site))


# --- R056 Error prevention ------------------------------------------------------

def test_r056_delete_without_confirm_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<button>Delete account</button>")})
    assert "R056" in _ids(content.judge(site))


def test_r056_delete_with_data_confirm_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page(
            '<button data-confirm="Delete this account?">Delete account'
            "</button>")})
    assert "R056" not in _ids(content.judge(site))


def test_r056_delete_with_confirm_in_js_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<button>Delete account</button>")})
    _write_js(tmp_path, "if (confirm('Delete this account?')) { remove(); }")
    assert "R056" not in _ids(content.judge(site))


# --- R057 Error recovery ---------------------------------------------------------

def test_r057_generic_error_string_in_js_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page("<h1>Hi</h1>")})
    _write_js(tmp_path, "alert('Something went wrong');")
    assert "R057" in _ids(content.judge(site))


def test_r057_bare_error_code_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<p>Error 0x80070057, please retry.</p>")})
    assert "R057" in _ids(content.judge(site))


def test_r057_specific_error_copy_passes(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page("<h1>Hi</h1>")})
    _write_js(tmp_path,
              "alert('Please enter a valid email address, like "
              "name@example.com.');")
    assert "R057" not in _ids(content.judge(site))


def test_r057_unlinked_error_message_flagged(tmp_path: Path):
    body = ('<form action="/go" method="post">'
            '<label for="email">Email</label>'
            '<input id="email" name="email" type="email" '
            'autocomplete="email">'
            '<p class="error">Enter a valid email address.</p>'
            '<button type="submit">Send message</button></form>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R057" in _ids(content.judge(site))


# --- R058 Minimalist --------------------------------------------------------------

def test_r058_three_competing_banners_flagged(tmp_path: Path):
    body = ('<div class="promo-banner">Sale</div>'
            '<div class="popup-banner">Offer</div>'
            '<div class="banner">News</div>'
            '<a class="btn-primary" href="/buy">Buy now</a>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R058" in _ids(content.judge(site))


def test_r058_one_banner_passes(tmp_path: Path):
    body = ('<div class="promo-banner">Sale</div>'
            '<a class="btn-primary" href="/buy">Buy now</a>')
    site = _site(tmp_path, {"index.html": _page(body)})
    assert "R058" not in _ids(content.judge(site))


# --- R059 Choice overload ----------------------------------------------------------

def test_r059_select_with_nine_options_flagged(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page(_select(9))})
    assert "R059" in _ids(content.judge(site))


def test_r059_eight_plans_without_recommended_flagged(tmp_path: Path):
    names = ["Basic", "Plus", "Pro", "Team", "Business", "Growth",
             "Scale", "Enterprise"]
    site = _site(tmp_path, {"index.html": _page(_plan_cards(names))})
    assert "R059" in _ids(content.judge(site))


def test_r059_eight_plans_with_recommended_passes(tmp_path: Path):
    names = ["Basic", "Plus", "Pro", "Team", "Business", "Growth",
             "Scale", "Enterprise"]
    site = _site(tmp_path, {
        "index.html": _page(_plan_cards(names, marked="Basic",
                                        marker="Recommended"))})
    assert "R059" not in _ids(content.judge(site))


# --- R060 Cognitive load ------------------------------------------------------------

def test_r060_checkout_prices_without_total_flagged(tmp_path: Path):
    body = "<p>Item $10</p><p>Shipping $5</p>"
    site = _site(tmp_path, {"checkout.html": _page(body)})
    assert "R060" in _ids(content.judge(site))


def test_r060_checkout_with_visible_total_passes(tmp_path: Path):
    body = "<p>Item $10</p><p>Shipping $5</p><p>Total: $15</p>"
    site = _site(tmp_path, {"checkout.html": _page(body)})
    assert "R060" not in _ids(content.judge(site))


# --- R061 Serial position ---------------------------------------------------------------

def test_r061_recommended_buried_mid_list_flagged(tmp_path: Path):
    names = ["Basic", "Plus", "Pro", "Team", "Enterprise"]
    site = _site(tmp_path, {
        "index.html": _page(_plan_cards(names, marked="Pro"))})
    assert "R061" in _ids(content.judge(site))


def test_r061_recommended_first_passes(tmp_path: Path):
    names = ["Basic", "Plus", "Pro", "Team", "Enterprise"]
    site = _site(tmp_path, {
        "index.html": _page(_plan_cards(names, marked="Basic",
                                        marker="Recommended"))})
    assert "R061" not in _ids(content.judge(site))


# --- R106 Information scent ---------------------------------------------------------------

def test_r106_click_here_link_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page('<a href="/guide">click here</a>')})
    assert "R106" in _ids(content.judge(site))


def test_r106_descriptive_link_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page('<a href="/guide">Read the setup guide</a>')})
    assert "R106" not in _ids(content.judge(site))


# --- R110 Trust ----------------------------------------------------------------------------

def _checkout_page(body: str) -> str:
    return _page(body)


def test_r110_hidden_costs_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "checkout.html": _checkout_page(
            "<p>Item $10</p><p>Shipping $5</p>")})
    assert "R110" in _ids(content.judge(site))


def test_r110_forced_account_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "checkout.html": _checkout_page(
            "<p>Item $10</p><p>Total: $10</p>"
            "<p>Create an account to continue to checkout.</p>")})
    assert "R110" in _ids(content.judge(site))


def test_r110_fabricated_scarcity_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "checkout.html": _checkout_page(
            "<p>Item $10</p><p>Total: $10</p>"
            "<p>Only 2 left in stock, hurry!</p>")})
    assert "R110" in _ids(content.judge(site))


def test_r110_missing_trust_signals_flagged(tmp_path: Path):
    site = _site(tmp_path, {
        "checkout.html": _checkout_page(
            "<p>Item $10</p><p>Total: $10</p>"
            '<a class="btn-primary" href="/pay">Pay now</a>')})
    assert "R110" in _ids(content.judge(site))


def test_r110_honest_checkout_passes(tmp_path: Path):
    site = _site(tmp_path, {
        "checkout.html": _checkout_page(
            "<p>Item $10</p><p>Shipping $5</p><p>Total: $15</p>"
            "<p>Continue as guest, or sign in.</p>"
            "<p>Free returns. Secure checkout. Read our reviews.</p>"
            '<a class="btn-primary" href="/pay">Pay now</a>')})
    assert "R110" not in _ids(content.judge(site))


def test_r110_does_not_fire_on_non_checkout_pages(tmp_path: Path):
    site = _site(tmp_path, {
        "index.html": _page("<p>Item $10</p><p>Only 2 left!</p>")})
    assert "R110" not in _ids(content.judge(site))


# --- Coverage invariant ---------------------------------------------------------------------

def test_checked_plus_unchecked_covers_content_rules(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page("<h1>Hi</h1><p>Short copy.</p>")})
    verdict = content.judge(site)
    owned = {r["id"] for r in rules_for_judger("content")}
    covered = set(verdict.measured["checked_rules"]) | set(verdict.unchecked)
    assert covered == owned


def test_clean_minimal_site_has_no_ux_findings(tmp_path: Path):
    site = _site(tmp_path, {"index.html": _page("<h1>Hi</h1><p>Short copy.</p>")})
    ux_rules = {f"R0{n}" for n in range(41, 62)} | {"R106", "R110"}
    assert not (ux_rules & _ids(content.judge(site)))
