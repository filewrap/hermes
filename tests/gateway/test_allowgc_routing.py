"""Behaviour contracts for /allowgc routing.

An active allowgc group runs in its group profile (``gc-<chat id>``): the
identity resolved at ingress carries the gc runtime, and an identity-less
source (restored row, hand-built source) resolves to the gc home through the
same fallback. A group with no allowgc profile still resolves to the launch
home. All state lives under a temp HERMES_HOME — never the real one.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.allowgc import create_group_profile
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.profile_routing import parse_profile_routes
from gateway.session import SessionSource
from gateway.session_identity import resolve_identity


class _Stub(BasePlatformAdapter):
    pass


_Stub.__abstractmethods__ = frozenset()


def _stub(platform, runner, label):
    adapter = _Stub.__new__(_Stub)
    adapter.platform, adapter.gateway_runner, adapter.label = platform, runner, label
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._pending_messages, adapter._active_sessions = {}, {}
    return adapter


def _runner(home, *, multiplex):
    """Bare GatewayRunner wired like tests/gateway/test_session_identity.py."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    runner.config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes([])
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {}
    runner._primary_profile_name = "default"
    primary = _stub(Platform.TELEGRAM, runner, "PRIMARY")
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {}
    return SimpleNamespace(runner=runner, home=home, primary=primary)


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


CHAT = "-100123456"
OWNER = "111"


def _group_source(chat_id=CHAT, user_id="222"):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="group", user_id=user_id)


def test_resolve_identity_allowgc_group_nonmultiplexed(profile_env):
    """Standalone gateway: runtime is the gc profile, authorization stays the
    launch home, multiplexed stays False (so the session-key profile is None)."""
    gc_home = create_group_profile(CHAT, "Group", OWNER)
    rig = _runner(profile_env, multiplex=False)
    source = rig.primary.build_source(chat_id=CHAT, chat_type="group", user_id="222")
    identity = resolve_identity(source, runner=rig.runner)

    assert identity.runtime_profile == "gc-100123456"
    assert identity.runtime_home == gc_home
    assert identity.transport_profile == "default"
    assert identity.authorization_home == profile_env
    assert identity.multiplexed is False
    assert identity.session_key_profile is None


def test_resolve_identity_allowgc_group_multiplexed(profile_env):
    """Multiplexed gateway: transport stays the receiving bot, runtime is gc."""
    gc_home = create_group_profile(CHAT, "Group", OWNER)
    rig = _runner(profile_env, multiplex=True)
    source = rig.primary.build_source(chat_id=CHAT, chat_type="group", user_id="222")
    identity = resolve_identity(source, runner=rig.runner)

    assert identity.runtime_profile == "gc-100123456"
    assert identity.runtime_home == gc_home
    assert identity.transport_profile == "default"
    assert identity.authorization_home == profile_env
    assert identity.multiplexed is True
    assert identity.session_key_profile == "gc-100123456"


def test_resolve_identity_allowgc_forum_topic(profile_env):
    gc_home = create_group_profile(CHAT, "Group", OWNER)
    rig = _runner(profile_env, multiplex=False)
    source = rig.primary.build_source(
        chat_id=CHAT, chat_type="forum", user_id="222", thread_id="77")
    identity = resolve_identity(source, runner=rig.runner)
    assert identity.runtime_profile == "gc-100123456"
    assert identity.runtime_home == gc_home


def test_resolve_identity_non_allowgc_group_stays_on_launch_home(profile_env):
    rig = _runner(profile_env, multiplex=False)
    source = rig.primary.build_source(chat_id="-100999", chat_type="group", user_id="222")
    identity = resolve_identity(source, runner=rig.runner)
    assert identity.runtime_profile == "default"
    assert identity.runtime_home == profile_env
    assert identity.authorization_home == profile_env


def test_resolve_profile_home_identity_less_allowgc_source(profile_env):
    """No pinned identity (restored row / hand-built source): the runner's
    home resolver still lands the turn in the gc profile."""
    gc_home = create_group_profile(CHAT, "Group", OWNER)
    rig = _runner(profile_env, multiplex=True)
    home = rig.runner._resolve_profile_home_for_source(_group_source())
    assert home == gc_home


def test_resolve_profile_home_non_allowgc_group_is_launch_home(profile_env):
    rig = _runner(profile_env, multiplex=True)
    home = rig.runner._resolve_profile_home_for_source(_group_source(chat_id="-100999"))
    assert home == profile_env


def test_resolve_profile_home_disabled_allowgc_is_launch_home(profile_env):
    from gateway.allowgc import disable_group_profile

    create_group_profile(CHAT, "Group", OWNER)
    disable_group_profile(CHAT)
    rig = _runner(profile_env, multiplex=True)
    assert rig.runner._resolve_profile_home_for_source(_group_source()) == profile_env
    source = rig.primary.build_source(chat_id=CHAT, chat_type="group", user_id="222")
    assert resolve_identity(source, runner=rig.runner).runtime_profile == "default"
