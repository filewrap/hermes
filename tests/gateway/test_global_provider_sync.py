"""Global provider sync: one launch-home provider store, visible in every profile.

Real-store tests (launch config.yaml + auth.json on disk, gc profile home on
disk, real snapshot call): an owner add in DM must appear and be usable in a
group, a member share must reach other members, an unshared member provider
must stay hidden, owner revoke must hide it again, key rotation must keep the
share flag, duplicate-URL adds must keep one definition with both keys and
the owner-first precedence, and owner delete must vanish everywhere.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session import SessionSource

OWNER = "111"
MEMBER_A = "222"
MEMBER_B = "333"
URL = "https://acme.example.invalid/v1"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Launch home + one gc profile home, HERMES_HOME pointed at launch."""
    import gateway.run as gateway_run

    launch = tmp_path / "launch"
    gc = launch / "profiles" / "gc-999"
    launch.mkdir()
    gc.mkdir(parents=True)
    (launch / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    (launch / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    (gc / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(gateway_run, "_hermes_home", launch)
    return SimpleNamespace(launch=launch, gc=gc)


def _runner(homes):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda _source: homes.gc
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-gc"
    return runner


def _group_source(user_id):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="999", user_id=user_id, chat_type="group",
    )


async def _group_snapshot(homes, user_id):
    runner = _runner(homes)
    providers, *_ = await runner._provider_inventory_snapshot(_group_source(user_id))
    return [row for row in providers if row["slug"] == "custom:acme"]


def _owner_add(key="owner-key-1"):
    """Owner adds the Acme endpoint from DM (launch home, no profile scope)."""
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None,
                          added_by=OWNER, is_owner=True)
    from hermes_cli.auth_commands import add_api_key_credential_for_ui

    add_api_key_credential_for_ui("custom:acme", key, added_by=OWNER)


def _launch_config_text(homes):
    path = homes.launch / "config.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _pool_rows(homes, provider="custom:acme"):
    import json

    path = homes.launch / "auth.json"
    if not path.exists():
        return []
    store = json.loads(path.read_text(encoding="utf-8"))
    return (store.get("credential_pool") or {}).get(provider) or []


@pytest.mark.asyncio
async def test_owner_dm_add_appears_and_is_usable_in_group(homes):
    from gateway.provider_sharing import provider_visible_to_user

    _owner_add()
    rows = await _group_snapshot(homes, MEMBER_B)
    assert len(rows) == 1
    assert rows[0]["name"] == "Acme"
    assert rows[0]["configured"] is True
    assert provider_visible_to_user("custom:acme", MEMBER_B) is True


@pytest.mark.asyncio
async def test_member_add_in_group_lands_in_global_store(homes):
    """A profile-less allowgc member adds by bare id: the definition and the
    key land in the launch store even though the add runs under gc scope, so
    no profile check or later migration is needed."""
    from gateway.run import _profile_runtime_scope
    from hermes_cli.main_provider_setup import _save_custom_provider

    with _profile_runtime_scope(homes.gc):
        _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
        from hermes_cli.auth_commands import add_api_key_credential_for_ui

        add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    assert "acme.example.invalid" in _launch_config_text(homes)
    assert not (homes.gc / "config.yaml").read_text(encoding="utf-8").count("acme.example.invalid")
    rows = _pool_rows(homes)
    assert [row.get("added_by") for row in rows] == [MEMBER_A]


@pytest.mark.asyncio
async def test_member_share_reaches_other_member(homes):
    from hermes_cli.auth_commands import set_provider_shared_for_ui

    _owner_add()
    # Member A adds a second key for the same endpoint, then shares it.
    from hermes_cli.auth_commands import add_api_key_credential_for_ui

    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    set_provider_shared_for_ui("custom:acme", MEMBER_A, True)
    rows = await _group_snapshot(homes, MEMBER_B)
    assert len(rows) == 1
    assert rows[0]["configured"] is True


@pytest.mark.asyncio
async def test_unshared_member_provider_hidden_from_others(homes):
    from gateway.provider_sharing import provider_visible_to_user
    from hermes_cli.auth_commands import add_api_key_credential_for_ui
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    assert await _group_snapshot(homes, MEMBER_B) == []
    assert await _group_snapshot(homes, MEMBER_A) != []
    assert provider_visible_to_user("custom:acme", MEMBER_B) is False
    assert provider_visible_to_user("custom:acme", MEMBER_A) is True


@pytest.mark.asyncio
async def test_owner_revoke_hides_shared_provider(homes):
    from gateway.provider_sharing import provider_visible_to_user
    from hermes_cli.auth_commands import (
        revoke_provider_shared_for_ui, set_provider_shared_for_ui,
    )
    from hermes_cli.auth_commands import add_api_key_credential_for_ui
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    set_provider_shared_for_ui("custom:acme", MEMBER_A, True)
    assert await _group_snapshot(homes, MEMBER_B) != []
    revoke_provider_shared_for_ui("custom:acme", OWNER)
    assert await _group_snapshot(homes, MEMBER_B) == []
    assert await _group_snapshot(homes, MEMBER_A) != []
    assert provider_visible_to_user("custom:acme", MEMBER_B) is False


@pytest.mark.asyncio
async def test_owner_revoke_refused_for_non_owner(homes):
    from hermes_cli.auth_commands import revoke_provider_shared_for_ui

    _owner_add()
    with pytest.raises((ValueError, PermissionError)):
        revoke_provider_shared_for_ui("custom:acme", MEMBER_A)


def test_key_rotation_keeps_shared_flag(homes):
    from hermes_cli.auth_commands import (
        add_api_key_credential_for_ui, set_provider_shared_for_ui,
    )
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "member-a-key-1", added_by=MEMBER_A)
    set_provider_shared_for_ui("custom:acme", MEMBER_A, True)
    add_api_key_credential_for_ui("custom:acme", "member-a-key-2", added_by=MEMBER_A)
    rows = _pool_rows(homes)
    assert len(rows) == 2
    assert all(bool(row.get("shared")) for row in rows)


def test_duplicate_url_adds_keep_one_definition_with_both_keys(homes):
    from hermes_cli.auth_commands import add_api_key_credential_for_ui
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "owner-key-1", added_by=OWNER)
    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    assert _launch_config_text(homes).count("acme.example.invalid") == 1
    rows = _pool_rows(homes)
    assert len(rows) == 2
    assert {row.get("added_by") for row in rows} == {OWNER, MEMBER_A}


def test_selection_precedence_owner_then_own_then_shared(homes):
    """Turn-time selection: owner's entry first, then the acting user's own,
    then a shared entry — never another user's unshared entry."""
    from agent.credential_pool import load_pool
    from agent.credential_pool_user_selection import bind_acting_user_context
    from hermes_cli.auth_commands import (
        add_api_key_credential_for_ui, set_provider_shared_for_ui,
    )
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None,
                          added_by=OWNER, is_owner=True)
    add_api_key_credential_for_ui("custom:acme", "owner-key-1", added_by=OWNER)
    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    add_api_key_credential_for_ui("custom:acme", "member-b-key", added_by=MEMBER_B)
    set_provider_shared_for_ui("custom:acme", MEMBER_B, True)

    def _selected(user_id):
        with bind_acting_user_context(user_id, owner_id=OWNER):
            pool = load_pool("custom:acme")
            entry = pool.select()
        assert entry is not None
        return entry.access_token

    # Owner's key serves everyone while the definition is owner-shared...
    assert _selected(MEMBER_A) == "owner-key-1"
    assert _selected(MEMBER_B) == "owner-key-1"
    assert _selected(OWNER) == "owner-key-1"


def test_selection_falls_back_to_own_then_shared_without_owner_entry(homes):
    from agent.credential_pool import load_pool
    from agent.credential_pool_user_selection import bind_acting_user_context
    from hermes_cli.auth_commands import (
        add_api_key_credential_for_ui, set_provider_shared_for_ui,
    )
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    add_api_key_credential_for_ui("custom:acme", "member-b-key", added_by=MEMBER_B)
    set_provider_shared_for_ui("custom:acme", MEMBER_A, True)

    def _selected(user_id):
        with bind_acting_user_context(user_id, owner_id=OWNER):
            return load_pool("custom:acme").select()

    assert _selected(MEMBER_A).access_token == "member-a-key"
    # B's own key beats A's shared key for B.
    assert _selected(MEMBER_B).access_token == "member-b-key"
    # A stranger sees only the shared entry.
    assert _selected("444").access_token == "member-a-key"


def test_selection_fails_closed_for_unshared_entries(homes):
    from agent.credential_pool import load_pool
    from agent.credential_pool_user_selection import bind_acting_user_context
    from hermes_cli.auth_commands import add_api_key_credential_for_ui
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None)
    add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    with bind_acting_user_context(MEMBER_B, owner_id=OWNER):
        assert load_pool("custom:acme").select() is None


@pytest.mark.asyncio
async def test_owner_delete_vanishes_everywhere(homes):
    import json

    from gateway.run import _profile_runtime_scope
    from hermes_cli.auth_commands import (
        add_api_key_credential_for_ui, remove_custom_provider_for_ui,
    )

    _owner_add()
    with _profile_runtime_scope(homes.gc):
        add_api_key_credential_for_ui("custom:acme", "member-a-key", added_by=MEMBER_A)
    assert len(_pool_rows(homes)) == 2
    with _profile_runtime_scope(homes.gc):
        removed = remove_custom_provider_for_ui("custom:acme")
    assert removed["removed_config"] is True
    assert "acme.example.invalid" not in _launch_config_text(homes)
    assert _pool_rows(homes) == []
    gc_auth = homes.gc / "auth.json"
    if gc_auth.exists():
        pool = json.loads(gc_auth.read_text(encoding="utf-8")).get("credential_pool") or {}
        assert "custom:acme" not in pool
    assert await _group_snapshot(homes, MEMBER_B) == []


@pytest.mark.asyncio
async def test_global_definition_wins_on_slug_conflict(homes):
    (homes.launch / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n"
        "providers:\n  acme:\n    name: Global Acme\n"
        "    base_url: https://global.example.invalid/v1\n"
        "    model: global-model\n",
        encoding="utf-8",
    )
    (homes.gc / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n"
        "providers:\n  acme:\n    name: Group Acme\n"
        "    base_url: https://group.example.invalid/v1\n"
        "    model: group-model\n",
        encoding="utf-8",
    )
    runner = _runner(homes)
    providers, *_ = await runner._provider_inventory_snapshot(_group_source(MEMBER_B))
    acme = next(row for row in providers if row["slug"] == "acme")
    assert acme["name"] == "Global Acme"
    assert acme["default_model"] == "global-model"


def test_model_switch_context_sees_global_definitions(homes):
    from gateway.slash_commands_model import _ModelSwitchContext

    _owner_add()
    ctx = _ModelSwitchContext(
        session_key="s",
        source=_group_source(MEMBER_B),
        config_path=homes.gc / "config.yaml",
        persist_global=False,
    )
    ctx.read_config()
    customs = ctx.custom_provs or []
    assert any(
        str(entry.get("base_url") or "") == URL for entry in customs
    ), customs


def test_owner_definition_stores_added_by_and_shared(homes):
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None,
                          added_by=OWNER, is_owner=True)
    text = _launch_config_text(homes)
    assert "added_by" in text
    assert OWNER in text
    assert "shared" in text


def test_definition_update_keeps_first_adder_and_shared(homes):
    from hermes_cli.main_provider_setup import _save_custom_provider

    _save_custom_provider(URL, "", "acme-model", None, "Acme", None,
                          added_by=MEMBER_A, is_owner=False)
    _save_custom_provider(URL, "", "other-model", None, "Acme", None,
                          added_by=MEMBER_B, is_owner=False)
    text = _launch_config_text(homes)
    assert text.count("acme.example.invalid") == 1
    assert MEMBER_A in text
    assert MEMBER_B not in text
