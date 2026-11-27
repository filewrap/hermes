"""Footer badges: the /footer fields as tappable badges (Beta).

The builder returns (label, callback data) pairs in footer order; the tap
answers carry the detail — provider + full model name, token counts, full
path — in the callback data itself, so no server-side lookup is needed.
"""

from __future__ import annotations

import pytest

from gateway.runtime_footer import (
    build_footer_badges,
    build_footer_html,
    footer_badges_enabled,
    footer_badges_html_buttons,
    footer_badges_link_style,
    format_runtime_footer,
)


def test_link_style_off_by_default():
    assert footer_badges_link_style({}, "telegram") is False
    assert footer_badges_link_style({"platforms": {"telegram": {}}}, "telegram") is False
    assert footer_badges_link_style(
        {"platforms": {"telegram": {"footer_badges": True}}}, "telegram") is False


def test_link_style_needs_the_badges_beta():
    """The New style toggle only shows while the Footer badges beta is on,
    and the helper reads as off without the beta — even when the key is set."""
    cfg = {"platforms": {"telegram": {"footer_badges": True, "footer_badges_link_style": True}}}
    assert footer_badges_link_style(cfg, "telegram") is True
    cfg_off = {"platforms": {"telegram": {"footer_badges": False, "footer_badges_link_style": True}}}
    assert footer_badges_link_style(cfg_off, "telegram") is False
    cfg_no_beta = {"platforms": {"telegram": {"footer_badges_link_style": True}}}
    assert footer_badges_link_style(cfg_no_beta, "telegram") is False


def test_link_style_is_telegram_only():
    cfg = {"platforms": {"telegram": {"footer_badges": True, "footer_badges_link_style": True}}}
    assert footer_badges_link_style(cfg, "telegram") is True
    assert footer_badges_link_style(cfg, "cli") is False
    assert footer_badges_link_style(cfg, None) is False


def test_badges_off_by_default():
    assert footer_badges_enabled({}, "telegram") is False
    assert footer_badges_enabled({"platforms": {"telegram": {}}}, "telegram") is False


def test_badges_are_telegram_only():
    cfg = {"platforms": {"telegram": {"footer_badges": True}}}
    assert footer_badges_enabled(cfg, "telegram") is True
    assert footer_badges_enabled(cfg, "cli") is False
    assert footer_badges_enabled(cfg, None) is False


def test_badges_mirror_the_footer_line(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = str(tmp_path / "projects" / "hermes")
    badges = build_footer_badges(
        model="openrouter/nousresearch/hermes-4-405b", provider="openrouter",
        context_tokens=38400, context_length=256000, cwd=cwd,
    )
    assert [label for label, _ in badges] == ["hermes-4-405b", "15%", "~/projects/hermes"]
    assert format_runtime_footer(
        model="openrouter/nousresearch/hermes-4-405b",
        context_tokens=38400, context_length=256000, cwd=cwd,
        fields=("model", "context_pct", "cwd")) == "hermes-4-405b · 15% · ~/projects/hermes"


def test_model_badge_carries_provider_and_full_name():
    assert build_footer_badges(
        model="nousresearch/hermes-4-405b", provider="agentrouter",
        context_tokens=0, context_length=None, cwd="",
    ) == [("hermes-4-405b", "fb:m:agentrouter|nousresearch/hermes-4-405b")]


def test_context_badge_carries_raw_counts():
    assert build_footer_badges(
        model="", provider="", context_tokens=38400, context_length=256000, cwd="",
    ) == [("15%", "fb:c:38400|256000")]


def test_cwd_badge_carries_home_relative_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert build_footer_badges(
        model="", provider="", context_tokens=0, context_length=None,
        cwd=str(tmp_path / "projects" / "hermes"),
    ) == [("~/projects/hermes", "fb:p:~/projects/hermes")]


def test_badge_data_never_exceeds_telegram_limit():
    badges = build_footer_badges(
        model="vendor/" + "x" * 100, provider="a-very-long-provider-name",
        context_tokens=0, context_length=None, cwd="",
    )
    assert badges
    for _, data in badges:
        assert len(data.encode("utf-8")) <= 64


def test_footer_html_buttons_are_inline_pills():
    """The badges are bare <tg-button> pills for nesting INLINE in the
    footer paragraph — the small in-body form the patches use. No
    <tg-button-row>: a row block renders as big keyboard-sized buttons."""
    buttons = footer_badges_html_buttons([("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
                                          ("14%", "fb:c:38080|272000")])
    assert buttons == ['<tg-button type="callback_data" style="primary" '
                       'data="fb:m:openai-codex|gpt-6-luna">gpt-6-luna</tg-button>',
                       '<tg-button type="callback_data" style="primary" '
                       'data="fb:c:38080|272000">14%</tg-button>']


def test_footer_html_buttons_skip_bad_entries():
    """Empty label, empty data, over-long data: skipped, never breaking the footer."""
    buttons = footer_badges_html_buttons(
        [("", "fb:m:x|y"), ("label", ""), ("ok", "x" * 65), ("good", "fb:m:x|y")])
    assert buttons == ['<tg-button type="callback_data" style="primary" '
                       'data="fb:m:x|y">good</tg-button>']
    assert footer_badges_html_buttons(
        [("", "fb:m:x|y"), ("label", ""), ("ok", "x" * 65)]) == []
    assert footer_badges_html_buttons([]) == []


def test_build_footer_html_nests_badges_inline():
    """The footer HTML is ONE paragraph whose content IS the badges —
    inline pills joined by " · ", the same form the settings patches use.
    No <tg-button-row> anywhere: a row block renders big."""
    html = build_footer_html(
        "gpt-6-luna · 14% · ~",
        [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
         ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")])
    assert html == ('<p><tg-button type="callback_data" style="primary" '
                    'data="fb:m:openai-codex|gpt-6-luna">gpt-6-luna</tg-button> · '
                    '<tg-button type="callback_data" style="primary" '
                    'data="fb:c:38080|272000">14%</tg-button> · '
                    '<tg-button type="callback_data" style="primary" '
                    'data="fb:p:~">~</tg-button></p>')
    assert "<tg-button-row" not in html


def test_footer_html_buttons_link_style():
    """New style on: the same badges render as link-style buttons."""
    buttons = footer_badges_html_buttons(
        [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"), ("14%", "fb:c:38080|272000")],
        style="link")
    assert buttons == ['<tg-button type="callback_data" style="link" '
                       'data="fb:m:openai-codex|gpt-6-luna">gpt-6-luna</tg-button>',
                       '<tg-button type="callback_data" style="link" '
                       'data="fb:c:38080|272000">14%</tg-button>']


def test_footer_html_buttons_bad_style_falls_back_to_primary():
    buttons = footer_badges_html_buttons(
        [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna")], style="bogus")
    assert buttons == ['<tg-button type="callback_data" style="primary" '
                       'data="fb:m:openai-codex|gpt-6-luna">gpt-6-luna</tg-button>']


def test_build_footer_html_link_style():
    html = build_footer_html(
        "gpt-6-luna · 14% · ~",
        [("gpt-6-luna", "fb:m:openai-codex|gpt-6-luna"),
         ("14%", "fb:c:38080|272000"), ("~", "fb:p:~")],
        style="link")
    assert html == ('<p><tg-button type="callback_data" style="link" '
                    'data="fb:m:openai-codex|gpt-6-luna">gpt-6-luna</tg-button> · '
                    '<tg-button type="callback_data" style="link" '
                    'data="fb:c:38080|272000">14%</tg-button> · '
                    '<tg-button type="callback_data" style="link" '
                    'data="fb:p:~">~</tg-button></p>')
    assert "<tg-button-row" not in html


def test_build_footer_html_without_badges_is_just_the_line():
    assert build_footer_html("gpt-6-luna · 14% · ~", []) == "gpt-6-luna · 14% · ~"
    assert build_footer_html("gpt-6-luna · 14% · ~",
                             [("", "fb:m:x|y")]) == "gpt-6-luna · 14% · ~"
