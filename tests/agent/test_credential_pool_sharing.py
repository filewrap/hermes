"""Per-user provider sharing: pool entry visibility and ownership flags.

A credential added by one user is theirs alone until they share it; entries
without ``added_by`` predate sharing (or came from host config) and stay
visible to everyone. ``added_by`` / ``shared`` live in ``PooledCredential.extra``
and must survive the auth.json round-trip.
"""

from agent.credential_pool import PooledCredential, entry_visible_to


def _entry(**extra):
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="key", auth_type="api_key",
        priority=0, source="manual", access_token="tok",
    )
    entry.extra.update(extra)
    return entry


def test_legacy_entry_without_owner_is_visible_to_everyone():
    entry = _entry()
    assert entry_visible_to(entry, "111") is True
    assert entry_visible_to(entry, "222") is True
    assert entry_visible_to(entry, None) is True


def test_own_entry_is_visible_and_others_entry_is_not():
    assert entry_visible_to(_entry(added_by="111"), "111") is True
    assert entry_visible_to(_entry(added_by="111"), "222") is False


def test_shared_entry_is_visible_to_other_users():
    assert entry_visible_to(_entry(added_by="111", shared=True), "222") is True
    assert entry_visible_to(_entry(added_by="111", shared=False), "222") is False


def test_ownership_flags_survive_auth_json_round_trip():
    entry = _entry(added_by="111", shared=True)
    restored = PooledCredential.from_dict("openrouter", entry.to_dict())
    assert restored.extra.get("added_by") == "111"
    assert restored.extra.get("shared") is True
    assert entry_visible_to(restored, "222") is True


def test_set_entry_extra_persists_sharing_flags(monkeypatch, tmp_path):
    """The admin mixin's extra writer persists the toggle across loads."""
    import os

    from agent.credential_pool import load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    os.makedirs(tmp_path, exist_ok=True)
    pool = load_pool("openrouter")
    entry = pool.add_entry(_entry())
    assert pool.set_entry_extra(entry.id, {"added_by": "111", "shared": True}) is not None

    reloaded = load_pool("openrouter").entries()
    assert len(reloaded) == 1
    assert reloaded[0].extra.get("added_by") == "111"
    assert reloaded[0].extra.get("shared") is True
    assert entry_visible_to(reloaded[0], "222") is True
