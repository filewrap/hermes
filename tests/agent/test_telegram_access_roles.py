import pytest

from agent.local_tenants import LocalTenantStore
from agent.mongo_tenants import Principal, TenantStoreError


OWNER = "9000"


def start(store, user_id, username=None, name=None):
    store.start_user(user_id)
    return store.record_started_identity(user_id, username, name)


def test_started_identity_is_durable_and_resolution_never_provisions(tmp_path):
    store = LocalTenantStore(tmp_path / "profile", OWNER)
    assert store.resolve_started_user("777") is None
    assert store.resolve_started_user("@notstarted") is None
    assert store._profiles.find_one({"_id": "tg-777"}) is None

    started = start(store, "42", "@Alice_1", "Alice Example")
    assert started == {"user_id": "42", "name": "Alice Example", "username": "alice_1", "start_count": 1}
    assert store.resolve_started_user("42") == started
    assert store.resolve_started_user("@ALICE_1") == started
    assert store.resolve_started_user("alice_1") == started
    store.start_user("42")
    # Identity persistence is not another /start counter.
    assert store.record_started_identity("42", "alice_1", "Alice Updated")["start_count"] == 2

    reopened = LocalTenantStore(tmp_path / "profile", OWNER)
    assert reopened.resolve_started_user("@alice_1")["name"] == "Alice Updated"
    assert reopened.resolve_started_user("99") is None


def test_only_owner_or_same_scope_sudo_can_grant_and_grants_do_not_leak(tmp_path):
    store = LocalTenantStore(tmp_path / "profile", OWNER)
    start(store, "101", "admin_1", "Scoped Admin")
    start(store, "202", "target_1", "Target")
    start(store, "303", "target_2", "Other Target")

    store.grant_sudo(Principal(OWNER), "@admin_1", "-1001")
    admin_group = store.principal_for_scope("101", "group", "-1001")
    store.ban_user(Principal(OWNER), "303", True)
    with pytest.raises(TenantStoreError, match="Banned Telegram user"):
        store.grant_sudo(Principal(OWNER), "303", "-1001")
    store.ban_user(Principal(OWNER), "303", False)
    with pytest.raises(TenantStoreError, match="across chats"):
        store.grant_sudo(admin_group, "202", "-1002")
    with pytest.raises(TenantStoreError, match="Scoped transport"):
        store.grant_sudo(Principal("101"), "202", "-1001")

    granted = store.grant_sudo(admin_group, "@target_1", "-1001")
    assert granted["user_id"] == "202"
    assert store.access_for_scope("202", "group", "-1001") == {
        "allowed": True, "banned": False, "sudo": True, "started": True,
    }
    assert store.access_for_scope("202", "group", "-1002")["allowed"] is False
    assert store.access_for_scope("202", "group", "-1002")["sudo"] is False
    assert store.access_for_scope("202", "dm", "202")["allowed"] is False
    assert store.access_for_scope("202", "dm", "202")["sudo"] is False
    with pytest.raises(TenantStoreError, match="Started Telegram user not found"):
        store.grant_sudo(admin_group, "404", "-1001")

    # A ban is an overriding denial even when the scoped grant remains durable.
    store.ban_user(Principal(OWNER), "202", True)
    assert store.access_for_scope("202", "group", "-1001") == {
        "allowed": False, "banned": True, "sudo": False, "started": True,
    }
    with pytest.raises(TenantStoreError, match="denied"):
        store.get(store.principal_for_scope("202", "group", "-1001"), "profile", "x")
    store.ban_user(Principal(OWNER), "202", False)
    store.revoke_sudo(Principal(OWNER), "202", "-1001")
    assert store.access_for_scope("202", "group", "-1001")["allowed"] is False
    store.grant_sudo(Principal(OWNER), "202", "-1001")
    store.allow_user(Principal(OWNER), "202", False)
    for chat_type, chat_id in (("dm", "202"), ("group", "-1001"), ("group", "-1002")):
        access = store.access_for_scope("202", chat_type, chat_id)
        assert access["allowed"] is False and access["sudo"] is False
    store.allow_user(Principal(OWNER), "202", True)
    assert store.access_for_scope("202", "dm", "202")["sudo"] is True
    store.allow_user(Principal(OWNER), "202", False)
    assert store.access_for_scope("202", "dm", "202")["allowed"] is False


def test_global_allowlist_admin_and_scope_roster_ignore_stale_yaml(tmp_path):
    home = tmp_path / "profile"
    store = LocalTenantStore(home, OWNER, allowed_user_ids=("101",))
    start(store, "101", "legacy_1", "Legacy Allowlisted")
    start(store, "202", "group_1", "Group User")
    store.grant_sudo(Principal(OWNER), "202", "-1001")
    store.ban_user(Principal(OWNER), "202", True)

    # The existing explicit allowlist retains its historical global admin rights.
    assert store.access_for_scope("101", "dm", "101")["sudo"] is True
    assert store.access_for_scope("101", "group", "-1001")["sudo"] is True
    assert store.access_for_scope("101", "group", "-1001")["allowed"] is True
    legacy_admin = store.principal_for_scope("101", "group", "-1001")
    with pytest.raises(TenantStoreError, match="Banned Telegram user"):
        store.grant_sudo(legacy_admin, "202", "-1001")

    # A stale YAML allowlist row must not be surfaced as active by the durable roster.
    (home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    chat_access:\n      groups:\n        '-1001':\n          allowed_users:\n            '999': {name: Stale YAML User}\n",
        encoding="utf-8",
    )
    roster = store.list_scope_users("group", "-1001")
    assert [row["user_id"] for row in roster] == ["101"]  # durable global allow survives; banned user does not
    assert all(item["user_id"] != "999" for item in roster)  # stale YAML entry is never authoritative

    store.ban_user(Principal(OWNER), "202", False)
    store.grant_sudo(legacy_admin, "202", "-1001")
    roster = store.list_scope_users("group", "-1001")
    assert {row["user_id"] for row in roster} == {"101", "202"}
    assert next(row for row in roster if row["user_id"] == "202") == {
        "user_id": "202", "name": "Group User", "username": "group_1", "start_count": 1,
    }
    assert all(item["user_id"] != OWNER and item["user_id"] != "999" for item in roster)


def test_group_allow_all_is_exact_group_scoped_and_cannot_bypass_bans(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "platforms:\n  telegram:\n    chat_access:\n      groups:\n        '-10055': {allow_all: true}\n",
        encoding="utf-8",
    )
    store = LocalTenantStore(home, OWNER)
    principal = store.principal_for_scope("501", "forum", "-10055")
    assert store.access_for_scope("501", "forum", "-10055") == {
        "allowed": True, "banned": False, "sudo": False, "started": False,
    }
    # Public group admission is carried by the scoped principal, not the plain
    # identity: data remains partitioned by user and never exposes owner data.
    owner = Principal(OWNER)
    store.put(owner, "settings", "owner-private", {"value": "owner"})
    store.put(principal, "settings", "only-me", {"value": 1})
    assert store.get(principal, "settings", "only-me") == {"value": 1}
    assert store.get(principal, "settings", "owner-private") is None
    with pytest.raises(TenantStoreError, match="denied"):
        store.get(store.principal_for_scope("501", "group", "-10056"), "settings", "only-me")
    with pytest.raises(TenantStoreError, match="denied"):
        store.get(store.principal_for_scope("501", "dm", "501"), "settings", "only-me")
    with pytest.raises(TenantStoreError, match="another group"):
        store.get_group_settings(principal, "-10056")
    with pytest.raises(TenantStoreError, match="another group"):
        store.set_group_settings(principal, "-10056", {"enabled": True})
    with pytest.raises(TenantStoreError, match="another group"):
        store.set_group_settings(store.principal_for_scope("501", "dm", "501"), "-10055", {"enabled": True})
    with pytest.raises(TenantStoreError, match="denied"):
        store.get(Principal("501"), "settings", "only-me")

    store.ban_user(Principal(OWNER), "501", True)
    assert store.access_for_scope("501", "group", "-10055")["allowed"] is False
    with pytest.raises(TenantStoreError, match="denied"):
        store.get(principal, "settings", "only-me")


def test_scoped_dm_grant_does_not_turn_global_allowed_into_group_sudo(tmp_path):
    store = LocalTenantStore(tmp_path / "profile", OWNER)
    start(store, "601", "admin_2", "Admin")
    start(store, "602", "target_2", "Target")
    store.grant_sudo(Principal(OWNER), "601", "dm")
    dm_admin = store.principal_for_scope("601", "private", "601")
    store.grant_sudo(dm_admin, "602", "dm")
    assert store.access_for_scope("602", "dm", "602")["sudo"] is True
    assert store.access_for_scope("602", "group", "-1007")["allowed"] is False
    assert store.access_for_scope("602", "group", "-1007")["sudo"] is False
