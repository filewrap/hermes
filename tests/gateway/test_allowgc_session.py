"""Behaviour contracts for the /allowgc shared session key.

``gateway.session.build_session_key`` is the single source of truth, so every
call site gets the shared key: in an allowgc group two different members key
the same session (the user segment is dropped, like forum topics). Plain
groups keep per-user keys; DMs and forum-topic keys are unchanged.
"""

from pathlib import Path

import pytest

from gateway.allowgc import create_group_profile
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key, is_shared_multi_user_session

OWNER = "111"
CHAT = "-100123456"


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Temp hermes root: Path.home() + HERMES_HOME both point at tmp_path.

    Mirrors the profile-test pattern in tests/hermes_cli/test_profiles.py so
    ``hermes_cli.profiles`` resolves gc- profiles under the temp dir.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _source(chat_id, chat_type, user_id, thread_id=None):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type=chat_type,
        user_id=user_id, thread_id=thread_id)


def test_allowgc_group_key_is_shared_across_members(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    first = build_session_key(_source(CHAT, "group", "222"))
    second = build_session_key(_source(CHAT, "group", "333"))
    assert first == second == "agent:main:telegram:group:-100123456"


def test_allowgc_group_key_shared_even_with_per_user_config(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    assert build_session_key(
        _source(CHAT, "group", "222"), group_sessions_per_user=True) == build_session_key(
        _source(CHAT, "group", "333"), group_sessions_per_user=True)


def test_plain_group_keeps_per_user_keys(profile_env):
    first = build_session_key(_source("-100999", "group", "222"))
    second = build_session_key(_source("-100999", "group", "333"))
    assert first == "agent:main:telegram:group:-100999:222"
    assert second == "agent:main:telegram:group:-100999:333"
    assert first != second


def test_dm_key_unchanged_by_allowgc(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    # A DM whose chat id matches a gc profile is still a DM key, not a group key.
    assert build_session_key(_source(CHAT, "dm", "222")) == "agent:main:telegram:dm:-100123456"
    assert build_session_key(_source(CHAT, "dm", "222")) == build_session_key(
        _source(CHAT, "dm", "333"))


def test_forum_topic_key_unchanged_by_allowgc(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    topic_a = build_session_key(_source(CHAT, "forum", "222", thread_id="77"))
    topic_b = build_session_key(_source(CHAT, "forum", "333", thread_id="77"))
    assert topic_a == topic_b == "agent:main:telegram:forum:-100123456:77"
    other_topic = build_session_key(_source(CHAT, "forum", "222", thread_id="78"))
    assert other_topic == "agent:main:telegram:forum:-100123456:78"


def test_forum_topic_key_unchanged_without_allowgc(profile_env):
    topic = build_session_key(_source("-100999", "forum", "222", thread_id="77"))
    assert topic == "agent:main:telegram:forum:-100999:77"


def test_disabled_allowgc_group_goes_back_to_per_user_keys(profile_env):
    from gateway.allowgc import disable_group_profile

    create_group_profile(CHAT, "Group", OWNER)
    assert build_session_key(_source(CHAT, "group", "222")) == build_session_key(
        _source(CHAT, "group", "333"))
    disable_group_profile(CHAT)
    assert build_session_key(_source(CHAT, "group", "222")) != build_session_key(
        _source(CHAT, "group", "333"))


def test_shared_label_follows_allowgc_group(profile_env):
    create_group_profile(CHAT, "Group", OWNER)
    assert is_shared_multi_user_session(_source(CHAT, "group", "222")) is True
    assert is_shared_multi_user_session(_source("-100999", "group", "222")) is False
    assert is_shared_multi_user_session(_source(CHAT, "dm", "222")) is False
