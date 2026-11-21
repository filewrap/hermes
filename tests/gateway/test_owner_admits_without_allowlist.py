"""The owner is admitted by the authorization home's .env itself.

On a fresh install there is no store allowlist, no env allowlist, and no
pairing approval yet. /start still answers the owner (the adapter reads
OWNER_TELEGRAM_ID from the profile .env directly), but the gateway's
authorization never read it, so /model — and every other gateway turn —
hit default-deny and handed the OWNER a pairing code. The owner id in the
launch home's .env is the grant: _principal_authorized must admit it
directly, Telegram-only, in DM and in groups, fail-closed when the owner
id is missing or invalid.

All state lives under a temp HERMES_HOME — never the real one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.profile_routing import parse_profile_routes
from gateway.session_identity import resolve_identity

OWNER = "100"
STRANGER = "202"
GROUP = "-100123"

_ALLOWLIST_ENV_VARS = (
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_USERS",
    "TELEGRAM_GROUP_ALLOWED_CHATS",
)


class _Stub(BasePlatformAdapter):
    pass


_Stub.__abstractmethods__ = frozenset()


def _stub(platform, runner, label):
    adapter = _Stub.__new__(_Stub)
    adapter.platform, adapter.gateway_runner, adapter.label = platform, runner, label
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._pending_messages, adapter._active_sessions = {}, {}
    return adapter


def _build_rig(tmp_path, monkeypatch, env_text):
    """Bare multiplexed GatewayRunner over a fresh-install launch home.

    Mirrors tests/gateway/test_allowgc_runner_authz.py: the scoped env
    reads go through the launch home's .env. The fresh install has ONLY
    the owner id — no store grant, no env allowlist, no pairing approval.
    """
    from agent import secret_scope
    from gateway.run import GatewayRunner

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in _ALLOWLIST_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes([])
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"
    primary = _stub(Platform.TELEGRAM, runner, "PRIMARY")
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {}
    served = [("default", home)]
    entered = patch("hermes_cli.profiles.profiles_to_serve", return_value=served)
    entered.start()
    try:
        yield SimpleNamespace(runner=runner, home=home, primary=primary)
    finally:
        entered.stop()
        secret_scope.set_multiplex_active(prev)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    yield from _build_rig(tmp_path, monkeypatch, "OWNER_TELEGRAM_ID=100\n")


@pytest.fixture
def rig_no_owner(tmp_path, monkeypatch):
    yield from _build_rig(tmp_path, monkeypatch, "")


@pytest.fixture
def rig_bad_owner(tmp_path, monkeypatch):
    yield from _build_rig(tmp_path, monkeypatch, "OWNER_TELEGRAM_ID=abc\n")


def _dm_source(rig, user_id):
    source = rig.primary.build_source(chat_id=user_id, chat_type="dm", user_id=user_id)
    resolve_identity(source, runner=rig.runner)
    return source


def _group_source(rig, user_id, chat_id=GROUP):
    source = rig.primary.build_source(chat_id=chat_id, chat_type="group", user_id=user_id)
    resolve_identity(source, runner=rig.runner)
    return source


def test_owner_admitted_in_dm_with_empty_store_no_allowlist_no_pairing(rig):
    """Fresh install: the owner in DM is admitted with nothing set up yet."""
    source = _dm_source(rig, OWNER)
    assert rig.runner._is_user_authorized_for_source(source) is True


def test_owner_admitted_in_group_with_empty_store_no_allowlist_no_pairing(rig):
    """Fresh install: the owner in a group is admitted with nothing set up yet."""
    source = _group_source(rig, OWNER)
    assert rig.runner._is_user_authorized_for_source(source) is True


def test_stranger_still_denied_under_same_empty_setup(rig):
    """No widening: a stranger is still denied in DM and in a group."""
    assert rig.runner._is_user_authorized_for_source(_dm_source(rig, STRANGER)) is False
    assert rig.runner._is_user_authorized_for_source(_group_source(rig, STRANGER)) is False


def test_missing_owner_id_changes_nothing(rig_no_owner):
    """Fail-closed: no owner id in the .env means the owner is denied too."""
    assert rig_no_owner.runner._is_user_authorized_for_source(
        _dm_source(rig_no_owner, OWNER)
    ) is False


def test_invalid_owner_id_changes_nothing(rig_bad_owner):
    """Fail-closed: a non-decimal owner id in the .env grants nobody."""
    assert rig_bad_owner.runner._is_user_authorized_for_source(
        _dm_source(rig_bad_owner, OWNER)
    ) is False
