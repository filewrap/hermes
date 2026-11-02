from __future__ import annotations

import stat

from plugins.platforms.telegram.topic_catalog import (
    forget_topic,
    known_topic_ids,
    remember_topic,
)


def test_topic_catalog_is_profile_scoped_across_a_b_a(tmp_path):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    owner = ("-100123", "42", "9001")

    remember_topic(home_a, *owner, 71)
    assert known_topic_ids(home_a, *owner) == [71]
    assert known_topic_ids(home_b, *owner) == []

    remember_topic(home_b, *owner, 82)
    remember_topic(home_b, *owner, 83)
    assert known_topic_ids(home_b, *owner) == [82, 83]

    # Switching homes back must return A's state, not the most recently used B state.
    assert known_topic_ids(home_a, *owner) == [71]
    assert stat.S_IMODE((home_a / ".telegram-known-topic-ids.json").stat().st_mode) == 0o600


def test_topic_catalog_validates_exact_chat_user_bot_identity(tmp_path):
    home = tmp_path / "profile"
    owner = ("-100123", "42", "9001")
    remember_topic(home, *owner, 71)
    remember_topic(home, "-100123", "43", "9001", 72)
    remember_topic(home, "-100123", "42", "9002", 73)
    remember_topic(home, "-100124", "42", "9001", 74)
    remember_topic(home, *owner, "not-a-numeric-topic")

    assert known_topic_ids(home, *owner) == [71]
    assert known_topic_ids(home, "-100123", "43", "9001") == [72]
    assert known_topic_ids(home, "-100123", "42", "9002") == [73]
    assert known_topic_ids(home, "-100124", "42", "9001") == [74]
    assert known_topic_ids(home, "-100123", "", "9001") == []
    assert known_topic_ids(home, "-100123", "42", None) == []

    # Forgetting is equally narrow; another bot's owner-scoped record remains intact.
    forget_topic(home, *owner, 71)
    assert known_topic_ids(home, *owner) == []
    assert known_topic_ids(home, "-100123", "42", "9002") == [73]
