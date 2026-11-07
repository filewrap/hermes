"""Behaviour contracts for the digest preflight gate."""

from __future__ import annotations

from plugins.platforms.telegram.digest_preflight import (
    blocking_failures,
    ok_to_send,
    preflight_digest,
)

COVER = {"id": "cover", "media": {"type": "photo", "media": "https://example.com/cover.jpg"}}


def _by_name(checks):
    return {check.name: check for check in checks}


def test_clean_flat_digest_passes_everything():
    checks = preflight_digest(
        "## Weekly Digest\n\nLead paragraph.\n\n- Item one\n- Item two\n",
        kind="markdown",
        sent_texts=[],
        resolve_link=lambda url: True,
    )
    assert ok_to_send(checks)
    assert blocking_failures(checks) == []
    assert {check.status for check in checks} <= {"pass"}


def test_preview_over_budget_warns_but_does_not_block():
    content = (
        "<h1>Title</h1><p>" + "word " * 200 + "</p>"
        "<details><summary>Full version</summary><p>Rest.</p></details>"
    )
    checks = _by_name(preflight_digest(content, kind="html", sent_texts=[]))
    assert checks["preview_budget"].status == "warn"
    assert checks["details_shape"].status == "pass"
    assert ok_to_send(preflight_digest(content, kind="html", sent_texts=[]))


def test_overlong_preview_paragraph_warns():
    content = (
        "<h1>Title</h1><p>" + "word " * 60 + "</p>"
        "<details><summary>Full version</summary><p>Rest.</p></details>"
    )
    checks = _by_name(preflight_digest(content, kind="html", sent_texts=[]))
    assert checks["preview_budget"].status == "warn"


def test_text_after_closing_details_blocks():
    content = "<h1>T</h1><details><summary>Full</summary><p>Rest.</p></details><p>Orphan.</p>"
    checks = _by_name(preflight_digest(content, kind="html", sent_texts=[]))
    assert checks["details_shape"].status == "fail"
    assert not ok_to_send(preflight_digest(content, kind="html", sent_texts=[]))
    assert [check.name for check in blocking_failures(preflight_digest(content, kind="html", sent_texts=[]))] == ["details_shape"]


def test_multiple_details_blocks_warn():
    content = "<details><summary>A</summary></details><details><summary>B</summary></details>"
    checks = _by_name(preflight_digest(content, kind="html", sent_texts=[]))
    assert checks["details_shape"].status == "warn"


def test_unbound_media_reference_blocks():
    checks = _by_name(
        preflight_digest("![](tg://photo?id=cover)", kind="markdown", sent_texts=[])
    )
    assert checks["media_bindings"].status == "fail"
    assert not ok_to_send(preflight_digest("![](tg://photo?id=cover)", kind="markdown", sent_texts=[]))


def test_bound_media_passes_and_unused_binding_blocks():
    bound = preflight_digest(
        "![](tg://photo?id=cover)", kind="markdown", media_bindings=[COVER], sent_texts=[]
    )
    assert _by_name(bound)["media_bindings"].status == "pass"
    unused = preflight_digest(
        "No media here.", kind="markdown", media_bindings=[COVER], sent_texts=[]
    )
    assert _by_name(unused)["media_bindings"].status == "fail"


def test_attach_reference_blocks_with_port_reason():
    checks = _by_name(
        preflight_digest("![x](attach://cover.jpg)", kind="markdown", sent_texts=[])
    )
    assert checks["attach_manifest"].status == "fail"
    assert "not supported" in checks["attach_manifest"].detail


def test_markdown_rule_without_blank_line_warns():
    content = "Some paragraph\n---\nMore text.\n"
    assert _by_name(preflight_digest(content, kind="markdown", sent_texts=[]))[
        "heading_rule_break"
    ].status == "warn"
    fixed = "Some paragraph\n\n---\nMore text.\n"
    assert _by_name(preflight_digest(fixed, kind="markdown", sent_texts=[]))[
        "heading_rule_break"
    ].status == "pass"
    html = "<p>Some paragraph</p><hr/><p>More text.</p>"
    assert _by_name(preflight_digest(html, kind="html", sent_texts=[]))[
        "heading_rule_break"
    ].status == "pass"


def test_bare_handles_flag_only_invalid_telegram_usernames():
    bad = "Ping @ab and @bad-name for details."
    assert _by_name(preflight_digest(bad, kind="markdown", sent_texts=[]))[
        "bare_handles"
    ].status == "warn"
    good = "Ping @valid_user for details, or mail user@example.com."
    assert _by_name(preflight_digest(good, kind="markdown", sent_texts=[]))[
        "bare_handles"
    ].status == "pass"


def test_outbound_links_need_a_resolver_and_dead_links_block():
    content = "See <a href=\"https://example.com/ok\">this</a> and https://example.com/dead."
    unchecked = _by_name(preflight_digest(content, kind="html", sent_texts=[]))
    assert unchecked["outbound_links"].status == "unchecked"
    alive = _by_name(
        preflight_digest(content, kind="html", sent_texts=[], resolve_link=lambda url: True)
    )
    assert alive["outbound_links"].status == "pass"
    dead = _by_name(
        preflight_digest(
            content,
            kind="html",
            sent_texts=[],
            resolve_link=lambda url: "dead" not in url,
        )
    )
    assert dead["outbound_links"].status == "fail"
    assert not ok_to_send(
        preflight_digest(
            content,
            kind="html",
            sent_texts=[],
            resolve_link=lambda url: "dead" not in url,
        )
    )


def test_duplicate_edition_blocks():
    content = "## Weekly Digest\n\nLead paragraph.\n"
    same = _by_name(
        preflight_digest(content, kind="markdown", sent_texts=["  ## Weekly Digest\n\nLead paragraph.  "])
    )
    assert same["duplicate"].status == "fail"
    different = _by_name(
        preflight_digest(content, kind="markdown", sent_texts=["## Other Digest\n\nElsewhere.\n"])
    )
    assert different["duplicate"].status == "pass"
    unknown = _by_name(preflight_digest(content, kind="markdown"))
    assert unknown["duplicate"].status == "unchecked"


def test_duplicate_check_reads_the_sent_index():
    from gateway import rich_sent_store

    rich_sent_store.record(111, 1, "## Weekly Digest\n\nLead paragraph.\n")
    rich_sent_store.record(111, 2, "## Other Digest\n\nElsewhere.\n")
    rich_sent_store.record(222, 1, "## Weekly Digest\n\nLead paragraph.\n")
    assert rich_sent_store.texts_for_chat(111) == [
        "## Weekly Digest\n\nLead paragraph.\n",
        "## Other Digest\n\nElsewhere.\n",
    ]
    assert rich_sent_store.texts_for_chat(222) == ["## Weekly Digest\n\nLead paragraph.\n"]
    assert rich_sent_store.texts_for_chat(999) == []
    assert rich_sent_store.texts_for_chat(None) == []

    content = "## Weekly Digest\n\nLead paragraph.\n"
    checks = _by_name(preflight_digest(content, kind="markdown", chat_id=111))
    assert checks["duplicate"].status == "fail"
    fresh = _by_name(preflight_digest(content, kind="markdown", chat_id=999))
    assert fresh["duplicate"].status == "pass"
