"""Access-gateway fail-closed contract: no allow-all path anywhere.

After the access-control redesign the ONLY access rule is owner-or-allowlist.
These tests prove, at the gateway/config layer, that:

(a) an allow-all switch configured by env var or YAML admits nobody;
(b) a platform configured with policy ``"open"`` admits only owner + allowlist;
(c) an owner-only command from an allowed non-owner is silently denied
    (covered in ``tests/gateway/test_tenant_commands.py``);
(d) startup with no owner configured warns loudly and still starts
    (covered in ``tests/gateway/test_own_policy_startup_gate.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "WEIXIN_ALLOWED_USERS",
        "WEIXIN_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_runner(platform: Platform, config: GatewayConfig, *, enforces: bool):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock(), enforces_own_access_policy=enforces)
    runner.adapters = {platform: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    return runner, adapter


def _source(platform: Platform, *, chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="some-user",
        chat_id="some-chat",
        user_name="tester",
        chat_type=chat_type,
    )


# ---------------------------------------------------------------------------
# (a) No allow-all access path remains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", [Platform.WEIXIN, Platform.WECOM, Platform.YUANBAO])
def test_allow_all_env_does_not_admit_non_allowlisted_sender(monkeypatch, platform):
    """``GATEWAY_ALLOW_ALL_USERS=true`` + ``dm_policy: open`` admits nobody.

    The env var is ignored entirely: a sender who is neither the owner nor on
    any allowlist is denied at the gateway, exactly as if the flag were unset.
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, extra={"dm_policy": "open"})}
    )
    runner, _adapter = _make_runner(platform, config, enforces=True)

    assert runner._is_user_authorized(_source(platform)) is False


def test_platform_allow_all_env_does_not_admit_non_allowlisted_sender(monkeypatch):
    """``DISCORD_ALLOW_ALL_USERS=true`` admits nobody on Discord either."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True)})
    runner, _adapter = _make_runner(Platform.DISCORD, config, enforces=False)

    assert runner._is_user_authorized(_source(Platform.DISCORD)) is False


# ---------------------------------------------------------------------------
# (b) "open" policy fails closed to the allowlist
# ---------------------------------------------------------------------------


def test_open_dm_policy_admits_only_allowlisted_sender(monkeypatch):
    """``dm_policy: open`` behaves as ``allowlist`` at the adapter intake.

    The opt-in env vars are gone, so setting them changes nothing: the
    allowlisted sender passes, every other sender is denied.
    """
    from plugins.platforms.weixin.adapter import WeixinAdapter

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("WEIXIN_ALLOW_ALL_USERS", "true")

    adapter = object.__new__(WeixinAdapter)
    adapter._dm_policy, adapter._group_policy = "open", "open"
    adapter._allow_from, adapter._group_allow_from = ["alice"], ["room-1"]

    assert adapter._is_dm_allowed("alice") is True
    assert adapter._is_dm_allowed("stranger") is False
    assert adapter._is_group_allowed("room-1") is True
    assert adapter._is_group_allowed("room-2") is False


def test_open_policy_with_empty_allowlist_admits_nobody(monkeypatch):
    """``open`` with no allowlist configured denies every sender."""
    from plugins.platforms.yuanbao.adapter import AccessPolicy

    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

    policy = AccessPolicy(
        dm_policy="open", dm_allow_from=[], group_policy="open", group_allow_from=[]
    )
    assert policy.is_dm_allowed("anyone") is False
    assert policy.is_group_allowed("any-room") is False
