"""Invariants for universal profiles (``gateway/user_profiles.py``).

One Hermes home per admitted Telegram user: the profile name is derived from the
numeric user id (``tg-<id>``), creation is idempotent and channel-stripped, and the
user's personal overlay moves out of the launch home into their own profile.
E2E against a temp HERMES_HOME (real ``create_profile``); no live gateway.
"""

from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.user_profiles import (
    ensure_user_profile,
    is_user_profile_name,
    maybe_seed_profiled_turn_history,
    migrate_personal_dir,
    resolve_user_profile,
    tenant_history_seed,
    user_profile_name,
)

OWNER_ID = "8495104954"
USER_ID = "123456789"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Temp launch home with an owner; ``Path.home()`` + ``HERMES_HOME`` agree."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch = tmp_path / ".hermes"
    launch.mkdir()
    (launch / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n", encoding="utf-8")
    (launch / "SOUL.md").write_text("# launch persona\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    return launch


def _source(*, chat_type="dm", user_id=USER_ID, platform=Platform.TELEGRAM, is_bot=False):
    return SessionSource(platform=platform, chat_id=user_id, chat_type=chat_type,
                         user_id=user_id, is_bot=is_bot)


def test_user_profile_name_is_the_numeric_id_only():
    assert user_profile_name(123456789) == "tg-123456789"
    assert user_profile_name("123456789") == "tg-123456789"
    assert user_profile_name("  123456789 ") == "tg-123456789"
    assert user_profile_name("someone") is None
    assert user_profile_name("@someone") is None
    assert user_profile_name("0") is None
    assert user_profile_name(0) is None
    assert user_profile_name(-5) is None
    assert user_profile_name("") is None
    assert user_profile_name(None) is None
    assert user_profile_name(True) is None
    assert is_user_profile_name("tg-123456789") is True
    assert is_user_profile_name("tg-someone") is False
    assert is_user_profile_name("default") is False
    assert is_user_profile_name("tg-") is False


def test_ensure_creates_an_empty_self_owned_profile(homes):
    home = ensure_user_profile(USER_ID, launch_home=homes)

    assert home is not None
    assert home == homes / "profiles" / f"tg-{USER_ID}"
    assert home.is_dir()
    env = (home / ".env").read_text(encoding="utf-8")
    assert f"OWNER_TELEGRAM_ID={USER_ID}" in env
    assert OWNER_ID not in env  # the launch owner's id never leaks in
    assert "TELEGRAM_API" not in env and "BOT_TOKEN" not in env
    assert (home / "SOUL.md").is_file()
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "# launch persona\n"
    assert (home / ".no-bundled-skills").is_file()
    from hermes_cli.profiles import profile_exists

    assert profile_exists(f"tg-{USER_ID}")
    # No bot credential, no adapter, no gateway identity files of its own.
    assert not (home / "gateway.pid").exists()
    assert not (home / "gateway_state.json").exists()


def test_ensure_is_idempotent_and_never_overwrites_the_profile(homes):
    first = ensure_user_profile(USER_ID, launch_home=homes)
    (first / "SOUL.md").write_text("my own persona", encoding="utf-8")
    (first / "notes.txt").write_text("mine", encoding="utf-8")

    second = ensure_user_profile(USER_ID, launch_home=homes)

    assert second == first
    assert (first / "SOUL.md").read_text(encoding="utf-8") == "my own persona"
    assert (first / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert (first / ".env").read_text(encoding="utf-8").count("OWNER_TELEGRAM_ID=") == 1


def test_ensure_refuses_invalid_user_ids(homes):
    assert ensure_user_profile("someone", launch_home=homes) is None
    assert ensure_user_profile("0", launch_home=homes) is None
    assert ensure_user_profile(None, launch_home=homes) is None
    assert not (homes / "profiles").exists()


def test_ensure_migrates_the_personal_overlay_into_the_profile(homes):
    personal = homes / "personal" / "telegram" / "users" / USER_ID
    (personal / "memory").mkdir(parents=True)
    (personal / "settings.json").write_text('{"memory_enabled": true}', encoding="utf-8")
    (personal / "memory" / "MEMORY.md").write_text("# my memories", encoding="utf-8")
    (personal / "memory" / "USER.md").write_text("# about me", encoding="utf-8")
    (personal / "skills").mkdir()
    (personal / "skills" / "my-skill").mkdir()
    (personal / "skills" / "my-skill" / "SKILL.md").write_text("# skill", encoding="utf-8")

    home = ensure_user_profile(USER_ID, launch_home=homes)

    assert home is not None
    moved = home / "personal" / "telegram" / "users" / USER_ID
    assert (moved / "settings.json").read_text(encoding="utf-8") == '{"memory_enabled": true}'
    assert (moved / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "# my memories"
    assert (moved / "memory" / "USER.md").read_text(encoding="utf-8") == "# about me"
    assert (moved / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8") == "# skill"
    # Exactly one live source: the launch copy is gone.
    assert not personal.exists()


def test_migrate_is_a_noop_without_a_personal_dir(homes):
    assert migrate_personal_dir(USER_ID, launch_home=homes, user_home=homes / "profiles" / "x") is True


def test_resolve_user_profile_routes_only_a_dm_on_the_primary_bot(homes):
    ensure_user_profile(USER_ID, launch_home=homes)
    kwargs = dict(transport_name="default", primary_profile="default", authorization_home=homes)

    assert resolve_user_profile(_source(), **kwargs) == f"tg-{USER_ID}"
    # Groups never route by sender, even for a profiled user.
    assert resolve_user_profile(_source(chat_type="group"), **kwargs) is None
    assert resolve_user_profile(_source(chat_type="forum"), **kwargs) is None
    assert resolve_user_profile(_source(chat_type="channel"), **kwargs) is None
    # The owner keeps the launch home (their profile).
    assert resolve_user_profile(_source(user_id=OWNER_ID), **kwargs) is None
    # Bots never get a profile.
    assert resolve_user_profile(_source(is_bot=True), **kwargs) is None
    # Another platform never routes.
    assert resolve_user_profile(_source(platform=Platform.DISCORD), **kwargs) is None
    # A secondary bot's own DM never takes the per-user branch.
    assert resolve_user_profile(_source(), transport_name="team_b",
                                primary_profile="default", authorization_home=homes) is None


def test_resolve_user_profile_needs_an_existing_home(homes):
    kwargs = dict(transport_name="default", primary_profile="default", authorization_home=homes)
    assert resolve_user_profile(_source(), **kwargs) is None
    ensure_user_profile(USER_ID, launch_home=homes)
    assert resolve_user_profile(_source(), **kwargs) == f"tg-{USER_ID}"


def test_resolve_user_profile_fails_closed_without_an_owner(homes, tmp_path):
    ensure_user_profile(USER_ID, launch_home=homes)
    ownerless = tmp_path / "ownerless"
    ownerless.mkdir()
    kwargs = dict(transport_name="default", primary_profile="default", authorization_home=ownerless)
    assert resolve_user_profile(_source(), **kwargs) is None


# --- Grant trigger (maybe_create_user_profile) --------------------------------


def _store(homes):
    from gateway.mongo_multiuser import store_for_home

    return store_for_home(homes)


def test_grant_for_a_started_user_creates_the_profile(homes):
    from gateway.mongo_multiuser import principal
    from gateway.user_profiles import maybe_create_user_profile

    store = _store(homes)
    store.start_user(USER_ID)
    store.allow_user(principal(OWNER_ID), USER_ID, True)

    home = maybe_create_user_profile(store, USER_ID)

    assert home == homes / "profiles" / f"tg-{USER_ID}"
    assert (home / ".env").read_text(encoding="utf-8").count(f"OWNER_TELEGRAM_ID={USER_ID}") == 1


def test_allow_command_creates_the_profile_for_a_started_user(homes):
    from gateway.mongo_multiuser import principal
    from plugins.platforms.telegram.multiuser import handle_command

    store = _store(homes)
    store.start_user(USER_ID)

    handled, response = handle_command(store, OWNER_ID, f"/allow {USER_ID}")

    assert handled is True
    assert "allowed" in response
    assert (homes / "profiles" / f"tg-{USER_ID}" / ".env").is_file()


def test_grant_without_start_creates_nothing(homes):
    from gateway.mongo_multiuser import principal
    from gateway.user_profiles import maybe_create_user_profile

    store = _store(homes)
    store.allow_user(principal(OWNER_ID), USER_ID, True)

    assert maybe_create_user_profile(store, USER_ID) is None
    assert not (homes / "profiles").exists()


def test_revoked_or_banned_user_gets_no_profile(homes):
    from gateway.mongo_multiuser import principal
    from gateway.user_profiles import maybe_create_user_profile

    store = _store(homes)
    store.start_user(USER_ID)
    store.allow_user(principal(OWNER_ID), USER_ID, False)
    assert maybe_create_user_profile(store, USER_ID) is None

    store.allow_user(principal(OWNER_ID), USER_ID, True)
    store.ban_user(principal(OWNER_ID), USER_ID, True)
    assert maybe_create_user_profile(store, USER_ID) is None
    assert not (homes / "profiles").exists()


def test_grant_for_the_owner_creates_nothing(homes):
    from gateway.user_profiles import maybe_create_user_profile

    store = _store(homes)
    assert maybe_create_user_profile(store, OWNER_ID) is None
    assert not (homes / "profiles").exists()


def test_grant_helper_never_raises(homes):
    from gateway.user_profiles import maybe_create_user_profile

    assert maybe_create_user_profile(None, USER_ID) is None
    assert maybe_create_user_profile(None, "not-an-id") is None


# --- Provider fallback (D6) ---------------------------------------------------


def _owner_config(home, monkeypatch):
    import json

    (home / "config.yaml").write_text(json.dumps({"model": {
        "provider": "custom", "base_url": "http://127.0.0.1:9/v1",
        "default": "owner-model", "api_key": "owner-key-123"}}))


def _profiled_runner(launch_home):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._authorization_home_for_source = lambda _source: launch_home
    return runner


def _profiled_source():
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.TELEGRAM, chat_id=USER_ID, chat_type="dm",
                         user_id=USER_ID, profile=f"tg-{USER_ID}")


def test_ensure_user_profile_strips_the_seeded_model_config(homes, monkeypatch):
    # create_profile seeds the launch profile's model block (provider,
    # endpoint, api_key) into every fresh profile; a user home must start
    # with no provider keys at all.
    _owner_config(homes, monkeypatch)
    user_home = ensure_user_profile(USER_ID, launch_home=homes)

    assert user_home is not None
    assert not (user_home / "config.yaml").exists()
    assert "owner-key-123" not in "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in user_home.rglob("*") if p.is_file()
    )
    # The launch config is untouched, and a second call does not resurrect
    # the seed (nor delete a config the user has since written).
    assert (homes / "config.yaml").exists()
    again = ensure_user_profile(USER_ID, launch_home=homes)
    assert again == user_home
    assert not (user_home / "config.yaml").exists()


def test_ensure_user_profile_migrates_settings_memory_and_skills(homes, monkeypatch):
    # The whole personal overlay moves: settings, memory, and personal skills.
    personal = homes / "personal" / "telegram" / "users" / USER_ID
    (personal / "memory").mkdir(parents=True)
    (personal / "memory" / "MEMORY.md").write_text("# user memory\n", encoding="utf-8")
    (personal / "skills" / "my-skill").mkdir(parents=True)
    (personal / "skills" / "my-skill" / "SKILL.md").write_text("# my skill\n", encoding="utf-8")
    (personal / "settings.json").write_text('{"memory_enabled": true}', encoding="utf-8")

    user_home = ensure_user_profile(USER_ID, launch_home=homes)

    assert user_home is not None
    moved = user_home / "personal" / "telegram" / "users" / USER_ID
    assert (moved / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "# user memory\n"
    assert (moved / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8") == "# my skill\n"
    assert (moved / "settings.json").exists()
    # One live source: the launch copies are gone.
    assert not personal.exists()


def _tenant_history_rows(homes, count=3):
    from gateway.tenant_runtime import room_key

    profile_key = str(Path(homes).expanduser().resolve(strict=False))
    rows = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}",
         "user_id": USER_ID, "chat_id": USER_ID, "thread_id": None, "profile": profile_key}
        for i in range(count)
    ]
    key = "history:" + room_key(USER_ID, USER_ID, None, profile=profile_key)
    return key, rows


def _launch_store_with_history(homes, count=3):
    """Launch store where the user is allowed, carrying ``count`` history rows."""
    from gateway.mongo_multiuser import principal, store_for_home

    store = store_for_home(homes)
    store.allow_user(principal(OWNER_ID), USER_ID, True)
    key, rows = _tenant_history_rows(homes, count=count)
    store.put(principal(USER_ID), "profile", key, rows)
    return store, key, rows


def test_tenant_history_seed_reads_the_launch_store_rows(homes, monkeypatch):
    store, _key, _rows = _launch_store_with_history(homes, count=45)

    seed = tenant_history_seed(store, USER_ID, USER_ID, None, homes)

    # The tenant window: last 40, normalized to plain {role, content}.
    assert len(seed) == 40
    assert seed[0] == {"role": "assistant", "content": "msg 5"}
    assert seed[-1] == {"role": "user", "content": "msg 44"}
    assert all(set(row) == {"role", "content"} for row in seed)


def test_tenant_history_seed_rejects_foreign_or_malformed_rows(homes, monkeypatch):
    from gateway.mongo_multiuser import principal, store_for_home

    store, key, rows = _launch_store_with_history(homes, count=2)
    rows[1] = {**rows[1], "user_id": "other-user"}
    store.put(principal(USER_ID), "profile", key, rows)
    assert tenant_history_seed(store, USER_ID, USER_ID, None, homes) == []

    store.put(principal(USER_ID), "profile", key, [{"role": "user"}])
    assert tenant_history_seed(store, USER_ID, USER_ID, None, homes) == []
    assert tenant_history_seed(store, "nobody", USER_ID, None, homes) == []


def test_maybe_seed_profiled_turn_history_applies_once(homes, monkeypatch):
    from gateway.mongo_multiuser import principal

    store, key, rows = _launch_store_with_history(homes, count=2)
    user_home = ensure_user_profile(USER_ID, launch_home=homes)

    first = maybe_seed_profiled_turn_history(_profiled_source(), homes, user_home)
    second = maybe_seed_profiled_turn_history(_profiled_source(), homes, user_home)

    assert first == [{"role": "user", "content": "msg 0"}, {"role": "assistant", "content": "msg 1"}]
    assert second == []
    assert (user_home / ".tenant-history-seeded").exists()
    # The launch store is never written: the old history stays preserved.
    assert store.get(principal(USER_ID), "profile", key) == rows


def test_seed_profiled_turn_history_method(homes, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin

    _store, _key, _rows = _launch_store_with_history(homes, count=1)
    user_home = ensure_user_profile(USER_ID, launch_home=homes)
    monkeypatch.setenv("HERMES_HOME", str(user_home))

    runner = _profiled_runner(homes)
    with _profile_runtime_scope(user_home):
        seed = GatewayTurnPrepareMixin._seed_profiled_turn_history(runner, source=_profiled_source())
        assert seed == [{"role": "user", "content": "msg 0"}]
        # Unprofiled source seeds nothing.
        assert GatewayTurnPrepareMixin._seed_profiled_turn_history(runner, source=_source()) == []

    runner_no_auth = object.__new__(type(runner))
    runner_no_auth._authorization_home_for_source = lambda _source: None
    with _profile_runtime_scope(user_home):
        assert GatewayTurnPrepareMixin._seed_profiled_turn_history(runner_no_auth, source=_profiled_source()) == []


def test_profiled_session_db_is_the_user_homes_state_db(homes, monkeypatch):
    from hermes_cli.web_server_sessions import _session_db_path_for_profile

    user_home = ensure_user_profile(USER_ID, launch_home=homes)
    assert _session_db_path_for_profile(f"tg-{USER_ID}") == user_home / "state.db"


def test_ensure_user_profile_strips_launch_browser_config(homes, monkeypatch):
    # D10: the launch config's absolute browser.user_data_dir and a real-profile
    # opt-in must never reach the user home — they would merge every user's
    # cookies/logins into one Chromium dir, or hand the user the owner's OS
    # profile. (The model seed never copies the browser: section in the first
    # place; the slice-6 strip removes the seeded file entirely.)
    import json

    (homes / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "base_url": "http://127.0.0.1:9/v1",
                  "default": "owner-model", "api_key": "owner-key-123"},
        "browser": {"user_data_dir": "/tmp/owner-chromium", "use_real_profile": True},
    }))
    user_home = ensure_user_profile(USER_ID, launch_home=homes)

    assert user_home is not None
    assert not (user_home / "config.yaml").exists()
    assert not any(
        "/tmp/owner-chromium" in p.read_text(encoding="utf-8", errors="ignore")
        or "use_real_profile" in p.read_text(encoding="utf-8", errors="ignore")
        for p in user_home.rglob("*") if p.is_file()
    )


def test_profiled_turn_browser_data_is_fresh_and_local(homes, monkeypatch):
    # D10: browser resolution is per-home. A fresh user home has no browser
    # config, so the turn gets the user's OWN persistent dir under their home
    # (browser.profile_persistence defaults to save on current main) — never
    # the owner's shared dir, no real profile. A user who later configures
    # their own dir gets their own path — never the owner's.
    import json

    from gateway.run import _profile_runtime_scope
    from tools.browser_session_cloud import use_real_profile, user_data_dir

    (homes / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "base_url": "http://127.0.0.1:9/v1",
                  "default": "owner-model", "api_key": "owner-key-123"},
        "browser": {"user_data_dir": "/tmp/owner-chromium", "use_real_profile": True},
    }))
    user_home = ensure_user_profile(USER_ID, launch_home=homes)

    with _profile_runtime_scope(user_home):
        assert user_data_dir() == str(user_home / "browser")
        assert use_real_profile() is False
    with _profile_runtime_scope(homes):
        assert user_data_dir() == "/tmp/owner-chromium"
        assert use_real_profile() is True

    # The user configures their own persistent dir: it resolves under their
    # home only, and the launch home's browser config is untouched.
    (user_home / "config.yaml").write_text(json.dumps({
        "browser": {"user_data_dir": "browser/chromium", "use_real_profile": False},
    }))
    with _profile_runtime_scope(user_home):
        assert user_data_dir() == str(user_home / "browser" / "chromium")
        assert use_real_profile() is False
    with _profile_runtime_scope(homes):
        assert user_data_dir() == "/tmp/owner-chromium"


def test_created_user_profile_is_enumerated_for_cron(homes, monkeypatch):
    # D12: per-user homes live under profiles/, so the host ticker's
    # profiles_to_serve() enumeration picks them up like any named profile.
    from hermes_cli.profiles import profiles_to_serve

    user_home = ensure_user_profile(USER_ID, launch_home=homes)
    served = dict(profiles_to_serve(multiplex=True))

    assert served.get(f"tg-{USER_ID}") == user_home


def test_created_user_profile_carries_no_gateway_identity_files(homes, monkeypatch):
    # D12: no gateway.pid / gateway_state.json in a user home — a stray one
    # would make _cron_profile_gate stand the host ticker down for that home.
    user_home = ensure_user_profile(USER_ID, launch_home=homes)

    assert not (user_home / "gateway.pid").exists()
    assert not (user_home / "gateway_state.json").exists()


def test_profiled_turn_resolves_the_owners_provider(homes, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin

    _owner_config(homes, monkeypatch)
    user_home = ensure_user_profile(USER_ID, launch_home=homes)
    # Ambient scope is the USER home, as a real turn runs it (no config, no
    # keys): the resolution can only succeed from the owner's scope.
    monkeypatch.setenv("HERMES_HOME", str(user_home))
    runner = _profiled_runner(homes)
    with _profile_runtime_scope(user_home):
        model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            runner, source=_profiled_source())

    assert model == "owner-model"
    assert runtime.get("api_key") == "owner-key-123"
    assert runtime.get("base_url") == "http://127.0.0.1:9/v1"
    # No owner key ever lands in the user home or the user turn's secret scope.
    assert "owner-key-123" not in (user_home / ".env").read_text(encoding="utf-8")
    from agent.secret_scope import current_secret_scope

    scope = current_secret_scope()
    assert scope is None or "owner-key-123" not in scope.values()


def test_profiled_turn_without_an_authorization_home_gets_no_owner_provider(homes, monkeypatch):
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin

    _owner_config(homes, monkeypatch)
    user_home = ensure_user_profile(USER_ID, launch_home=homes)
    monkeypatch.setenv("HERMES_HOME", str(user_home))
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._authorization_home_for_source = lambda _source: None

    # No auth home: the resolution stays under the user home, which has no
    # provider at all — the ladder raises before any API call, and the error
    # names the user's own .env, never the owner's key.
    import pytest

    with _profile_runtime_scope(user_home):
        with pytest.raises(RuntimeError, match="not connected to any AI provider"):
            GatewayTurnPrepareMixin._resolve_session_agent_runtime(
                runner, source=_profiled_source())
