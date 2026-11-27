"""Acknowledgement fast path (beta): bare "thanks" / "ok" / "👍" messages
answered with a reaction and no model call.

Behaviour contracts: the matcher accepts only bare acknowledgements from a
small explicit set (plus 1–3 bare emoji) and rejects everything else —
answers, questions, commands, links, longer text. The decision needs every
guard: bare ack, beta on, reaction available, and nothing waiting (no
pending approval, no answerable pending clarify, not an answer to a
question). The setting reader mirrors the threaded-replies reader: the live
runtime attr wins over the persisted config value.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.ack_fast_path import (
    ack_fast_path_enabled,
    is_acknowledgement_only,
    should_fast_path,
)


@pytest.mark.parametrize(
    "text",
    [
        "thanks",
        "Thanks!",
        "THANKS",
        "  ok  ",
        "okay",
        "thank you",
        "thx",
        "ty",
        "shukriya",
        "k",
        "chalo",
        "theek hai",
        "theek",
        "got it",
        "noted",
        "👍",
        "👍👍",
        "👍👍👍",
        "❤️",
        "🔥",
        "🙏",
    ],
)
def test_acknowledgement_only_matches_bare_acks(text):
    assert is_acknowledgement_only(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "yes",  # an answer, not an acknowledgement
        "yeah",
        "haan",
        "great",  # ambiguous — can answer "is it good?"
        "good",
        "perfect",
        "ok thanks",  # two phrases, not one bare ack
        "thanks, and restart",
        "thank you so much",
        "ok 2",
        "ok?",
        "thanks?",
        "/thanks",
        "https://example.com",
        "see https://example.com",
        "",
        "   ",
        "hello",
        "👍 thanks",  # emoji plus words
        "please restart",
        "ok, restart the gateway",
    ],
)
def test_acknowledgement_only_rejects_everything_else(text):
    assert is_acknowledgement_only(text) is False


def test_acknowledgement_only_rejects_non_text():
    assert is_acknowledgement_only(None) is False
    assert is_acknowledgement_only(42) is False
    assert is_acknowledgement_only(["thanks"]) is False


def _decision(**overrides):
    base = {
        "is_ack": True,
        "setting_on": True,
        "has_reaction": True,
        "has_pending_approval": False,
        "has_answerable_pending_clarify": False,
        "reply_answers_question": False,
    }
    base.update(overrides)
    return should_fast_path(**base)


def test_fast_path_decision_needs_every_guard():
    assert _decision() is True
    for flag in (
        "is_ack",
        "setting_on",
        "has_reaction",
    ):
        assert _decision(**{flag: False}) is False
    for flag in (
        "has_pending_approval",
        "has_answerable_pending_clarify",
        "reply_answers_question",
    ):
        assert _decision(**{flag: True}) is False


def _adapter(*, extra=None, runtime=None):
    adapter = SimpleNamespace(config=SimpleNamespace(extra=extra))
    if runtime is not None:
        adapter._ack_fast_path_runtime = runtime
    return adapter


def test_setting_reader_runtime_attr_wins_over_config():
    assert ack_fast_path_enabled(_adapter(extra={"ack_fast_path": True})) is True
    assert ack_fast_path_enabled(_adapter(extra={"ack_fast_path": False})) is False
    assert ack_fast_path_enabled(_adapter(extra={})) is False
    assert ack_fast_path_enabled(_adapter(extra=None)) is False
    assert ack_fast_path_enabled(SimpleNamespace()) is False
    # Live toggle wins both ways, exactly like threaded replies.
    assert ack_fast_path_enabled(_adapter(extra={"ack_fast_path": False}, runtime=True)) is True
    assert ack_fast_path_enabled(_adapter(extra={"ack_fast_path": True}, runtime=False)) is False
