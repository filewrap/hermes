"""Behaviour contracts for scoped (allowgc) grants on the Mongo tenant store.

``LocalTenantStore`` has always carried ``grant_scope_access`` /
``access_for_scope``; the ``MongoTenantStore`` base did not, so on a
Mongo-backed group home the adapter's auto-admission grant — and the
runner gate's group-store read — failed with ``AttributeError`` and the
member was silently denied. The scoped grant must work identically on
both backends: group scope only, no sudo, no global allow.
"""
from __future__ import annotations

import pytest

from agent.mongo_tenants import (
    MongoTenantStore,
    Principal,
    ScopedPrincipal,
    TenantStoreError,
)

OWNER = "9000"
MEMBER = "201"
OTHER_MEMBER = "202"
GROUP = "-100123"
OTHER_GROUP = "-100456"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    import mongomock

    db = mongomock.MongoClient().get_database("hermes")
    return MongoTenantStore(tmp_path, OWNER, database=db)


def test_grant_scope_access_admits_group_only_without_sudo(store):
    owner = Principal(OWNER)

    store.grant_scope_access(owner, MEMBER, GROUP)

    access = store.access_for_scope(MEMBER, "group", GROUP)
    assert access["allowed"] is True
    assert access["sudo"] is False
    assert access["banned"] is False
    # No global allow, no other group, no DM access from the scoped grant.
    assert store.get_access(MEMBER) == {"allowed": False, "banned": False}
    assert store.access_for_scope(MEMBER, "group", OTHER_GROUP)["allowed"] is False
    assert store.access_for_scope(MEMBER, "dm", "dm")["allowed"] is False


def test_grant_scope_access_is_idempotent(store):
    owner = Principal(OWNER)

    store.grant_scope_access(owner, MEMBER, GROUP)
    store.grant_scope_access(owner, MEMBER, GROUP)

    assert store.access_for_scope(MEMBER, "group", GROUP)["allowed"] is True
    assert store.access_for_scope(MEMBER, "group", GROUP)["sudo"] is False


def test_grant_scope_access_refuses_banned_target(store):
    owner = Principal(OWNER)
    store.ban_user(owner, MEMBER, True)

    with pytest.raises(TenantStoreError):
        store.grant_scope_access(owner, MEMBER, GROUP)

    assert store.access_for_scope(MEMBER, "group", GROUP)["allowed"] is False


def test_grant_scope_access_refuses_owner_target(store):
    with pytest.raises(TenantStoreError):
        store.grant_scope_access(Principal(OWNER), OWNER, GROUP)


def test_grant_scope_access_requires_scope_admin(store):
    stranger = Principal(OTHER_MEMBER)

    with pytest.raises(TenantStoreError):
        store.grant_scope_access(stranger, MEMBER, GROUP)

    assert store.access_for_scope(MEMBER, "group", GROUP)["allowed"] is False


def test_grant_scope_access_scoped_admin_cannot_grant_across_chats(store):
    owner = Principal(OWNER)
    store.grant_scope_access(owner, MEMBER, GROUP)
    # A sudo holder in this group: global allow + this group's sudo scope.
    store._set_profile(MEMBER, {"allowed": True, "sudo_scopes": ["group:" + GROUP]})
    scoped = ScopedPrincipal(MEMBER, "group", GROUP)

    with pytest.raises(TenantStoreError):
        store.grant_scope_access(scoped, OTHER_MEMBER, OTHER_GROUP)
    assert store.access_for_scope(OTHER_MEMBER, "group", OTHER_GROUP)["allowed"] is False

    # The same scoped admin CAN grant inside its own group.
    store.grant_scope_access(scoped, OTHER_MEMBER, GROUP)
    assert store.access_for_scope(OTHER_MEMBER, "group", GROUP)["allowed"] is True


def test_access_for_scope_ban_overrides_scoped_grant(store):
    owner = Principal(OWNER)
    store.grant_scope_access(owner, MEMBER, GROUP)
    assert store.access_for_scope(MEMBER, "group", GROUP)["allowed"] is True

    store.ban_user(owner, MEMBER, True)

    access = store.access_for_scope(MEMBER, "group", GROUP)
    assert access["allowed"] is False
    assert access["banned"] is True
    assert access["sudo"] is False


def test_access_for_scope_owner_is_allowed_without_grant(store):
    access = store.access_for_scope(OWNER, "group", GROUP)
    assert access["allowed"] is True
    assert access["sudo"] is True
