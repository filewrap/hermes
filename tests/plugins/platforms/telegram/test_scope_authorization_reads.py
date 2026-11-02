"""Independent grants need fresh tenant reads, not unrelated group YAML parsing."""
from types import SimpleNamespace

import pytest

from agent.mongo_tenants import Principal
from gateway.config import PlatformConfig
from gateway.mongo_multiuser import store_for_home
from plugins.platforms.telegram.client_operations import TelegramClientOperations
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.telegram_chat_settings import update_chat_settings


def make_source(home, user_id="200"):
    return SimpleNamespace(home=home, chat_id="-100", chat_type="group", user_id=user_id,
                           thread_id=None, is_bot=False, is_guest=False)


def make_operations():
    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda source: source.home)
    return TelegramClientOperations(adapter)


@pytest.mark.parametrize("grant", ["owner", "global", "scoped"])
def test_independent_grants_do_not_read_group_config_but_still_see_live_bans(
    tmp_path, monkeypatch, grant,
):
    homes = [tmp_path / name for name in ("a", "b")]
    for home in homes:
        home.mkdir()
        (home / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
        store = store_for_home(home)
        if grant == "global":
            store.allow_user(Principal("100"), "200", True)
        elif grant == "scoped":
            store.start_user("200")
            store.record_started_identity("200", "tester", "Tester")
            store.grant_sudo(Principal("100"), "200", "-100")
        update_chat_settings(home, "group", "-100", {"allow_all": False})

    from hermes_cli import config
    read_config = config.read_user_config_raw
    reads = []

    def counted_read(path, *args, **kwargs):
        reads.append(path)
        return read_config(path, *args, **kwargs)

    monkeypatch.setattr(config, "read_user_config_raw", counted_read)
    ops = make_operations()
    uid = "100" if grant == "owner" else "200"
    for home in (homes[0], homes[1], homes[0]):
        assert ops._authorize(make_source(home, uid), owner=False)[0] is True
    assert reads == []

    if grant != "owner":
        # A separate store writes a ban; the same operations object must see it
        # immediately, even though no group config is consulted.
        writer = store_for_home(homes[0])
        writer.ban_user(Principal("100"), uid, True)
        assert ops._authorize(make_source(homes[0], uid), owner=False)[0] is False
        assert ops._authorize(make_source(homes[1], uid), owner=False)[0] is True
        writer.ban_user(Principal("100"), uid, False)
        assert ops._authorize(make_source(homes[0], uid), owner=False)[0] is True
        assert reads == []


def test_guest_policy_and_revoked_grants_remain_live_and_exact_group_scoped(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("OWNER_TELEGRAM_ID=100\n")
    writer = store_for_home(home)
    ops = make_operations()
    source = make_source(home)

    assert ops._authorize(source, owner=False)[0] is False
    update_chat_settings(home, "group", "-200", {"allow_all": True})
    assert ops._authorize(source, owner=False)[0] is False
    update_chat_settings(home, "group", "-100", {"allow_all": True})
    assert ops._authorize(source, owner=False)[0] is True
    update_chat_settings(home, "group", "-100", {"allow_all": False})
    assert ops._authorize(source, owner=False)[0] is False

    writer.allow_user(Principal("100"), "200", True)
    assert ops._authorize(source, owner=False)[0] is True
    writer.allow_user(Principal("100"), "200", False)
    assert ops._authorize(source, owner=False)[0] is False
    update_chat_settings(home, "group", "-100", {"allow_all": True})
    assert ops._authorize(source, owner=False)[0] is True
    writer.ban_user(Principal("100"), "200", True)
    assert ops._authorize(source, owner=False)[0] is False
    writer.ban_user(Principal("100"), "200", False)
    assert ops._authorize(source, owner=False)[0] is True
    update_chat_settings(home, "group", "-100", {"allow_all": False})
    assert ops._authorize(source, owner=False)[0] is False
