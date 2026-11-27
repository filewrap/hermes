"""Bot-bypass and own-policy startup behaviour read the plugin's PlatformEntry, not a core table.

A platform plugin that declares ``allow_bots_env`` / ``own_policy_env`` gets the same
authorization behaviour core's bundled platforms do. There is no allow-all switch and no
startup refusal for access reasons: an "open" policy fails closed to the allowlist.
"""

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource

_NAME = "authzprobe"


@pytest.fixture
def probe_platform(monkeypatch):
    from gateway.platform_registry import PlatformEntry, platform_registry

    for var in ("AUTHZPROBE_ALLOW_BOTS", "AUTHZPROBE_DM_POLICY", "AUTHZPROBE_GROUP_POLICY",
                "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(var, raising=False)
    platform_registry.register(PlatformEntry(
        name=_NAME, label="Authz Probe", adapter_factory=lambda cfg: None, check_fn=lambda: True,
        allow_bots_env="AUTHZPROBE_ALLOW_BOTS",
        own_policy_env=("AUTHZPROBE_DM_POLICY", "AUTHZPROBE_GROUP_POLICY"),
    ))
    yield Platform(_NAME)
    platform_registry.unregister(_NAME)


def test_declared_allow_bots_env_admits_bot_without_user_id(probe_platform, monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    source = SessionSource(platform=probe_platform, chat_id="c1", chat_type="group", user_id=None,
                           user_name="", is_bot=True)
    assert runner._is_user_authorized(source) is False
    monkeypatch.setenv("AUTHZPROBE_ALLOW_BOTS", "all")
    assert runner._is_user_authorized(source) is True


def test_declared_own_policy_env_open_policy_fails_closed_without_allowlist(probe_platform, monkeypatch):
    """An "open" group policy on a declared own-policy platform admits nobody
    who is not allowlisted — and never refuses startup, even with the (dead)
    allow-all env var set."""
    from gateway.run import GatewayRunner

    cfg = GatewayConfig(platforms={probe_platform: PlatformConfig(enabled=True)})
    runner = GatewayRunner(cfg)
    assert runner._start_check_access_policy() is False
    monkeypatch.setenv("AUTHZPROBE_GROUP_POLICY", "open")
    assert runner._start_check_access_policy() is False
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    assert runner._start_check_access_policy() is False

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    source = SessionSource(platform=probe_platform, chat_id="c1", chat_type="group", user_id="stranger",
                           user_name="", is_bot=False)
    assert runner._is_user_authorized(source) is False


@pytest.mark.parametrize("declared", ["both", "group_only", "none"])
def test_declared_group_chat_env_and_pairing_suppression(monkeypatch, declared):
    """``group_allowed_chats_env`` authorizes group chats by id; only ``allowlist_suppresses_pairing``
    turns the unauthorized-DM default from "pair" into "ignore" (a group list alone never does)."""
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.run import GatewayRunner

    for var in ("GRPPROBE_ALLOWED_USERS", "GRPPROBE_GROUP_CHATS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(var, raising=False)
    fields = {"both": dict(group_allowed_chats_env="GRPPROBE_GROUP_CHATS", allowlist_suppresses_pairing=True),
              "group_only": dict(group_allowed_chats_env="GRPPROBE_GROUP_CHATS"), "none": {}}[declared]
    platform_registry.register(PlatformEntry(
        name="grpprobe", label="Group Probe", adapter_factory=lambda cfg: None, check_fn=lambda: True,
        allowed_users_env="GRPPROBE_ALLOWED_USERS", **fields))
    try:
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(platforms={Platform("grpprobe"): PlatformConfig(enabled=True)})
        runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
        group = SessionSource(platform=Platform("grpprobe"), chat_id="g1", chat_type="group", user_id=None,
                              user_name="")
        monkeypatch.setenv("GRPPROBE_GROUP_CHATS", "g1")
        assert runner._is_user_authorized(group) is (declared != "none")
        dm = runner._get_unauthorized_dm_behavior(Platform("grpprobe"))
        assert dm == ("ignore" if declared == "both" else "pair")
        monkeypatch.delenv("GRPPROBE_GROUP_CHATS")
        monkeypatch.setenv("GRPPROBE_ALLOWED_USERS", "owner")
        assert runner._get_unauthorized_dm_behavior(Platform("grpprobe")) == ("ignore" if declared == "both" else "pair")
    finally:
        platform_registry.unregister("grpprobe")
