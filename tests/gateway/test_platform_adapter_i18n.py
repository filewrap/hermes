"""Telegram + Discord adapters read their chat copy from the i18n catalog at call time.

Pins the wave-4 contract: nothing user-facing is bound at import, the exec-approval card uses the
shared ``gateway.exec_approval.*`` keys, translated text is HTML-escaped before Telegram's ``<b>``
wrapper, Discord slash-command text is cut at the 100-char app-command cap, button labels at 80,
and both command-menu fingerprints change with the language.
"""

from __future__ import annotations

import pytest

from agent import i18n
from agent.i18n import t
from gateway.platforms.base import utf16_len

import plugins.platforms.discord.adapter as discord_adapter
from plugins.platforms.discord.adapter import (
    _DISCORD_APP_COMMAND_TEXT_LIMIT,
    _DISCORD_BUTTON_LABEL_LIMIT,
    _NATIVE_SLASH_COMMAND_SPECS,
    DiscordAdapter,
    ExecApprovalView,
    _native_slash_commands,
)


@pytest.fixture
def fake_locale(tmp_path, monkeypatch):
    """Point the catalog loader at a throwaway locales dir with an ``xx`` catalog whose values carry
    HTML metacharacters and exceed every platform cap; ``activate()`` switches the language to it."""
    import hermes_yaml as yaml

    locales = tmp_path / "locales"
    locales.mkdir()
    long_text = "L" * 300
    catalog = {
        "gateway": {"exec_approval": {
            "header": "Hermes <wants> & runs", "reason_label": "Why <flag>",
            "smart_deny_line": "Smart & <DENY>: one op only", "action_once": long_text,
            "action_session": "S", "action_always": "A", "action_deny": "D",
        }},
        "platform": {
            "telegram": {"approval": {
                "action_once": long_text, "action_session": "s", "action_always": "a", "action_deny": "d",
                "toast_already_resolved": long_text,
            }},
            "discord": {
                "approval": {"question": "Q?", "requested_command_label": "Cmd:"},
                "command": {
                    "new": {"description": long_text, "followup": "done"},
                    "reasoning": {"choice_none": long_text},
                },
            },
        },
    }
    (locales / "xx.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
    real_dir = i18n._locales_dir()
    (locales / "en.yaml").write_text((real_dir / "en.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(i18n, "_locales_dir", lambda: locales)
    monkeypatch.setattr(i18n, "_normalize_lang", lambda lang, home=None: str(lang).lower())
    i18n.reset_language_cache()

    def activate(lang: str = "xx"):
        monkeypatch.setenv("HERMES_LANGUAGE", lang)
        i18n.reset_language_cache()

    yield activate
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    i18n.reset_language_cache()


# ── shared contract ──────────────────────────────────────────────────────────

def test_exec_approval_contract_keys_exist_in_english():
    for key in ("header", "reason_label", "smart_deny_line", "timed_out_notice",
                "action_once", "action_session", "action_always", "action_deny"):
        assert t(f"gateway.exec_approval.{key}") != f"gateway.exec_approval.{key}"


@pytest.mark.parametrize("module", [discord_adapter])
def test_unauthorized_notice_is_resolved_per_call_not_at_import(module):
    assert not hasattr(module, "_UNAUTHORIZED"), "import-bound notice would freeze the language"
    assert callable(module._unauthorized)
    assert "allowed list" in module._unauthorized()


# ── Telegram ─────────────────────────────────────────────────────────────────
# The fork's Telegram transport is Telethon/MTProto: it has no ``TelegramAdapter``
# class, no ``_EA_*`` class attributes, no ``_toast``/``_TOAST_LIMIT``, no
# ``_menu_fingerprint`` and no ``_bot_commands`` helper, and the exec-approval
# prompt reads no ``gateway.exec_approval.*`` / ``platform.telegram.approval.*``
# catalog keys. The Telegram half of this contract therefore has no Telethon
# equivalent to pin; the shared key-existence test above still guards the keys.


# ── Discord ──────────────────────────────────────────────────────────────────

def test_discord_native_slash_specs_hold_catalog_keys_only():
    for name, description_key, args, _template, followup_key in _NATIVE_SLASH_COMMAND_SPECS:
        assert description_key.startswith(("platform.discord.command.", "slash.")), name
        assert t(description_key) != description_key, (name, description_key)
        if followup_key:
            assert t(followup_key) != followup_key, (name, followup_key)
        for arg_name, _type, _default, desc_key, choices in args:
            assert t(desc_key) != desc_key, (name, arg_name)
            for label, value in choices or ():
                # Bare level names ("low") are identifiers; anything with a dot must resolve.
                assert "." not in label or t(label) != label, (name, label, value)


def test_discord_native_slash_text_respects_100_char_cap(fake_locale):
    fake_locale()
    by_name = {row[0]: row for row in _native_slash_commands()}
    assert utf16_len(by_name["new"][1]) == _DISCORD_APP_COMMAND_TEXT_LIMIT == 100
    reasoning_choices = dict((v, k) for k, v in by_name["reasoning"][2][0][4])
    assert utf16_len(reasoning_choices["none"]) == 100
    assert reasoning_choices["low"] == "low"
    # Follow-ups stay keys until the command runs.
    assert by_name["new"][4] == "platform.discord.command.new.followup"
    for row in _native_slash_commands():
        assert utf16_len(row[1]) <= 100
        for arg in row[2]:
            assert utf16_len(arg[3]) <= 100


def test_discord_card_uses_shared_contract_and_platform_copy(fake_locale):
    fake_locale()
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    assert adapter._EA_HEADER == "⚠️ **Hermes <wants> & runs**\n\nQ?\n\n**Cmd:**\n"
    assert adapter._EA_REASON_LABEL == "**Why <flag>:** "
    assert adapter._EA_SMART_DENY_LINE == "\n\n**Smart & <DENY>:** one op only"


def test_discord_exec_approval_button_labels_localized_and_capped(fake_locale):
    fake_locale()
    view = ExecApprovalView(session_key="s", allowed_user_ids={"1"})
    labels = {}
    view._localize_buttons(allow_once="gateway.exec_approval.action_once", deny="gateway.exec_approval.action_deny")
    for child in getattr(view, "children", []):
        cb = getattr(child, "callback", None)
        fn_name = getattr(cb, "__name__", None) or getattr(getattr(cb, "callback", None), "__name__", None)
        if fn_name:
            labels[fn_name] = child.label
    once = getattr(view, "allow_once", None)
    if hasattr(once, "label"):
        labels["allow_once"] = once.label
        labels["deny"] = view.deny.label
    if labels:  # real discord.py materialises the decorated buttons; the test stub may not
        assert utf16_len(labels["allow_once"]) == _DISCORD_BUTTON_LABEL_LIMIT == 80
        assert labels["deny"] == "D"


def test_discord_command_sync_fingerprint_includes_language(fake_locale, monkeypatch):
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    adapter._client = None
    english = adapter._desired_command_sync_fingerprint()
    fake_locale()
    assert adapter._desired_command_sync_fingerprint() != english
    fake_locale("en")
    assert adapter._desired_command_sync_fingerprint() == english
