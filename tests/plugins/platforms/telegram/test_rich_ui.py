"""Blue callback badges that sit right after a setting name.

A badge is a blue nested rich callback button labelled ``new`` or
``beta`` with no brackets: tapping it answers a toast, it is never a copy
pill, and it carries no disabled rendering.
"""
import pytest

from plugins.platforms.telegram.rich_ui import _BADGE_TOASTS, _settings_badge


def test_new_badge_is_a_blue_callback_button_without_brackets():
    badge = _settings_badge("new", "ps:1:badge:new")
    assert badge == ('<tg-button type="callback_data" style="primary" '
                     'data="ps:1:badge:new">new</tg-button>')
    assert "[" not in badge
    assert "copy_text" not in badge
    assert "disabled" not in badge


def test_beta_badge_is_a_blue_callback_button_without_brackets():
    badge = _settings_badge("beta", "ps:1:badge:beta")
    assert badge == ('<tg-button type="callback_data" style="primary" '
                     'data="ps:1:badge:beta">beta</tg-button>')
    assert "[" not in badge


def test_unknown_badge_kind_is_rejected():
    with pytest.raises(ValueError):
        _settings_badge("soon", "ps:1:badge:soon")


def test_badge_toasts_name_the_kind():
    assert _BADGE_TOASTS == {"new": "new function — enjoy", "beta": "beta function — enjoy"}
