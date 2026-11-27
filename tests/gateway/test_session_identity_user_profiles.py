"""Invariants for the universal-profiles routing rule in ``resolve_identity``.

A Telegram DM from a user with a per-user home (``tg-<id>``) runs under that home;
everything else — unprofiled DMs, groups, the owner, explicit routes — keeps today's
routing. Real profile dirs on disk; only ``get_profile_dir`` / ``profiles_to_serve``
are patched to the temp home (``profile_exists`` is real, so the existence gate is
exercised, not assumed).
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.profile_routing import parse_profile_routes
from gateway.session_identity import identity_of, resolve_identity
from hermes_constants import mark_named_profile_deleted

OWNER_ID = "8495104954"
USER_ID = "123456789"


class _Stub(BasePlatformAdapter):
    pass


_Stub.__abstractmethods__ = frozenset()


def _stub(platform, runner, label):
    adapter = _Stub.__new__(_Stub)
    adapter.platform, adapter.gateway_runner, adapter.label = platform, runner, label
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._pending_messages, adapter._active_sessions = {}, {}
    return adapter


def _runner(home, *, routes=()):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes(list(routes))
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"
    primary = _stub(Platform.TELEGRAM, runner, "PRIMARY")
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {"ops": {}}
    return SimpleNamespace(runner=runner, home=home, primary=primary)


@pytest.fixture
def mux(tmp_path, monkeypatch):
    """Launch home (owner 8495104954) + a live user profile + a live ``ops`` profile.

    The owner's own ``tg-<owner>`` dir exists too, so the owner test proves the
    owner exclusion, not just a missing directory.
    """
    home = tmp_path / "hh"
    (home / "profiles" / f"tg-{USER_ID}").mkdir(parents=True)
    (home / "profiles" / f"tg-{USER_ID}" / ".env").write_text(
        f"OWNER_TELEGRAM_ID={USER_ID}\n", encoding="utf-8")
    (home / "profiles" / f"tg-{OWNER_ID}").mkdir(parents=True)
    (home / "profiles" / f"tg-{OWNER_ID}" / ".env").write_text(
        f"OWNER_TELEGRAM_ID={OWNER_ID}\n", encoding="utf-8")
    (home / "profiles" / "ops").mkdir(parents=True)
    (home / "profiles" / "ops" / ".env").write_text("", encoding="utf-8")
    (home / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER_ID}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    served = [("default", home), (f"tg-{USER_ID}", home / "profiles" / f"tg-{USER_ID}"),
              (f"tg-{OWNER_ID}", home / "profiles" / f"tg-{OWNER_ID}"),
              ("ops", home / "profiles" / "ops")]
    rig = _runner(home)
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), \
            patch("hermes_cli.profiles.get_profile_dir",
                  side_effect=lambda n: home if n == "default" else home / "profiles" / n):
        yield rig


def test_profiled_dm_routes_to_the_user_home(mux):
    source = mux.primary.build_source(chat_id=USER_ID, chat_type="dm", user_id=USER_ID)
    identity = resolve_identity(source, runner=mux.runner)

    assert identity.transport_profile == "default"
    assert identity.runtime_profile == f"tg-{USER_ID}"
    assert identity.authorization_home == mux.home
    assert identity.runtime_home == mux.home / "profiles" / f"tg-{USER_ID}"
    assert identity.store_path == mux.home / "profiles" / f"tg-{USER_ID}" / "state.db"
    assert identity_of(source) is identity
    assert mux.primary._source_session_key(source) == f"agent:tg-{USER_ID}:telegram:dm:{USER_ID}"
    assert mux.runner._session_key_for_source(source) == mux.primary._source_session_key(source)
    assert mux.runner._resolve_profile_home_for_source(source) == mux.home / "profiles" / f"tg-{USER_ID}"


def test_unprofiled_dm_stays_on_the_launch_home(mux):
    source = mux.primary.build_source(chat_id="555", chat_type="dm", user_id="555")
    identity = resolve_identity(source, runner=mux.runner)

    assert (identity.transport_profile, identity.runtime_profile) == ("default", "default")
    assert identity.runtime_home == mux.home
    assert mux.primary._source_session_key(source) == "agent:main:telegram:dm:555"


def test_group_from_a_profiled_user_stays_on_the_launch_home(mux):
    source = mux.primary.build_source(chat_id="-100123", chat_type="group", user_id=USER_ID)
    identity = resolve_identity(source, runner=mux.runner)

    assert (identity.transport_profile, identity.runtime_profile) == ("default", "default")
    assert identity.runtime_home == mux.home
    assert mux.primary._source_session_key(source) == "agent:main:telegram:group:-100123:123456789"


def test_owner_dm_stays_on_the_launch_home_even_with_an_owner_named_dir(mux):
    source = mux.primary.build_source(chat_id=OWNER_ID, chat_type="dm", user_id=OWNER_ID)
    identity = resolve_identity(source, runner=mux.runner)

    assert (identity.transport_profile, identity.runtime_profile) == ("default", "default")
    assert identity.runtime_home == mux.home


def test_explicit_route_beats_the_user_profile(mux):
    # Route this user's DM chat_id to "ops" — the route wins even though a
    # per-user home exists.
    mux.runner.config.profile_routes = parse_profile_routes([
        {"name": "ops-dm", "platform": "telegram", "profile": "ops", "chat_id": USER_ID},
    ])
    source = mux.primary.build_source(chat_id=USER_ID, chat_type="dm", user_id=USER_ID)
    identity = resolve_identity(source, runner=mux.runner)

    assert identity.runtime_profile == "ops"
    assert identity.runtime_home == mux.home / "profiles" / "ops"


def test_deleted_user_profile_stops_routing(mux):
    mark_named_profile_deleted(mux.home / "profiles" / f"tg-{USER_ID}")
    source = mux.primary.build_source(chat_id=USER_ID, chat_type="dm", user_id=USER_ID)
    identity = resolve_identity(source, runner=mux.runner)

    assert (identity.transport_profile, identity.runtime_profile) == ("default", "default")
    assert identity.runtime_home == mux.home
