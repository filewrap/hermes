"""Platform authorization reads must honor the per-profile secret scope (#93522).

Under ``gateway.multiplex_profiles`` every secondary profile is constructed
inside ``_profile_runtime_scope`` and its ``.env`` lives in that scope —
``gateway/run.py`` explicitly does NOT mutate ``os.environ`` with it. Raw
``os.getenv`` authorization reads therefore (a) silently miss the secondary
profile's own allowlists (fail-closed no-replies) and (b) leak the default
profile's ``GATEWAY_ALLOW_ALL_USERS`` / allowlists into secondary profiles
(fail-open admissions).

Covers the scoped env helpers of weixin / yuanbao / signal / wecom plus the
fail-closed admission gates built on them (an ``"open"`` policy behaves as
``"allowlist"``; no ``*_ALLOW_ALL_USERS`` opt-in exists). Canonical shape
reference: QQ's ``_resolve_qq_secret``.
"""

import contextlib
import os

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope


@contextlib.contextmanager
def _scope(secrets):
    token = set_secret_scope(secrets)
    try:
        yield
    finally:
        reset_secret_scope(token)


@pytest.fixture
def profile_scope():
    """Install a secondary-profile secret scope; os.environ stays 'default'."""
    with _scope(
        {
            "WEIXIN_DM_POLICY": "allowlist",
            "WEIXIN_ALLOWED_USERS": "wx-alice",
            "WEIXIN_ALLOW_ALL_USERS": "",
            "YUANBAO_DM_POLICY": "open",
            "SIGNAL_ALLOWED_USERS": "sig-bob",
            "WECOM_DM_POLICY": "allowlist",
            "WECOM_ALLOWED_USERS": "wecom-carol",
        }
    ):
        yield


# ─────────────────────────────────────────────────────────────────────
# Scoped helpers: read the scope, never leak os.environ past it
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "module_path,helper_name,var",
    [
        ("plugins.platforms.weixin.adapter", "_wx_secret", "WEIXIN_ALLOWED_USERS"),
        ("plugins.platforms.yuanbao.adapter", "_yb_secret", "YUANBAO_DM_POLICY"),
        ("plugins.platforms.signal.adapter", "_sig_secret", "SIGNAL_ALLOWED_USERS"),
        ("plugins.platforms.wecom.adapter", "_get_scoped_secret", "WECOM_ALLOWED_USERS"),
    ],
)
def test_helper_reads_profile_scope_first(module_path, helper_name, var, monkeypatch):
    """Scope value wins over anything in os.environ."""
    monkeypatch.setenv(var, "FROM-DEFAULT-ENV")
    module = __import__(module_path, fromlist=[helper_name])
    helper = getattr(module, helper_name)

    with _scope({var: "from-profile-scope"}):
        assert helper(var, "") == "from-profile-scope"


@pytest.fixture
def multiplex_on(monkeypatch):
    """Simulate a multiplexed deployment: scoped misses fail closed.

    With multiplexing off, a scope miss deliberately falls through to
    os.environ (the cron-overlay contract) — the cross-profile leak only
    exists while the multiplexer is active, which is exactly the #93522
    scenario.
    """
    monkeypatch.setattr("agent.secret_scope._MULTIPLEX_ACTIVE", True)


@pytest.mark.parametrize(
    "module_path,helper_name",
    [
        ("plugins.platforms.weixin.adapter", "_wx_secret"),
        ("plugins.platforms.yuanbao.adapter", "_yb_secret"),
        ("plugins.platforms.signal.adapter", "_sig_secret"),
        ("plugins.platforms.wecom.adapter", "_get_scoped_secret"),
    ],
)
def test_helper_does_not_leak_default_env_into_scoped_miss(
    module_path, helper_name, multiplex_on, monkeypatch
):
    """Scoped miss must return the default, NOT fall through to os.environ.

    This is the fail-closed half of #93522: the default profile's
    ``*_ALLOW_ALL_USERS=true`` in process env must not answer a secondary
    profile's gate.
    """
    monkeypatch.setenv("WEIXIN_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("YUANBAO_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "*")
    monkeypatch.setenv("WECOM_ALLOW_ALL_USERS", "true")

    module = __import__(module_path, fromlist=[helper_name])
    helper = getattr(module, helper_name)

    with _scope({}):  # scope installed, keys absent
        assert helper("WEIXIN_ALLOW_ALL_USERS", "") == ""
        assert helper("YUANBAO_ALLOW_ALL_USERS", "") == ""
        assert helper("WECOM_ALLOW_ALL_USERS", "") == ""
        # signal's "*" default means open at adapter level — the scoped
        # read must still not see the unscoped value:
        assert helper("SIGNAL_ALLOWED_USERS", "restricted-default") == "restricted-default"


@pytest.mark.parametrize(
    "module_path,helper_name,var",
    [
        ("plugins.platforms.weixin.adapter", "_wx_secret", "WEIXIN_ALLOWED_USERS"),
        ("plugins.platforms.yuanbao.adapter", "_yb_secret", "YUANBAO_DM_POLICY"),
        ("plugins.platforms.signal.adapter", "_sig_secret", "SIGNAL_ALLOWED_USERS"),
        ("plugins.platforms.wecom.adapter", "_get_scoped_secret", "WECOM_ALLOWED_USERS"),
    ],
)
def test_helper_falls_back_to_environ_without_scope(module_path, helper_name, var, monkeypatch):
    """Single-profile (no scope installed): os.environ remains authoritative."""
    monkeypatch.setenv(var, "legacy-env-value")
    module = __import__(module_path, fromlist=[helper_name])
    helper = getattr(module, helper_name)
    assert helper(var, "") == "legacy-env-value"


# ─────────────────────────────────────────────────────────────────────
# Admission gates built on the helpers
# ─────────────────────────────────────────────────────────────────────


def test_weixin_open_gate_ignores_default_env_under_scope(profile_scope, multiplex_on, monkeypatch):
    """Default profile sets GATEWAY_ALLOW_ALL_USERS=true; secondary scope does
    NOT carry it — the gate must stay closed. "open" behaves as allowlist: the
    allowlisted sender passes, strangers never do."""
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

    from plugins.platforms.weixin.adapter import WeixinAdapter

    adapter = WeixinAdapter.__new__(WeixinAdapter)
    adapter._dm_policy, adapter._group_policy = "open", "open"
    adapter._allow_from, adapter._group_allow_from = ["wx-alice"], ["room-1"]
    assert adapter._is_dm_allowed("wx-alice") is True
    assert adapter._is_dm_allowed("stranger") is False
    assert adapter._is_group_allowed("room-1") is True
    assert adapter._is_group_allowed("room-2") is False


def test_weixin_open_gate_ignores_scope_opt_in():
    """Even the profile's own scoped ``WEIXIN_ALLOW_ALL_USERS`` opt-in grants
    nothing now: "open" is allowlist-only."""
    from plugins.platforms.weixin.adapter import WeixinAdapter

    with _scope({"WEIXIN_ALLOW_ALL_USERS": "yes"}):
        adapter = WeixinAdapter.__new__(WeixinAdapter)
        adapter._dm_policy, adapter._group_policy = "open", "open"
        adapter._allow_from, adapter._group_allow_from = ["wx-alice"], []
        assert adapter._is_dm_allowed("wx-alice") is True
        assert adapter._is_dm_allowed("stranger") is False


def test_yuanbao_access_policy_gate_ignores_default_env(profile_scope, multiplex_on, monkeypatch):
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

    from plugins.platforms.yuanbao.adapter import AccessPolicy

    policy = AccessPolicy(
        dm_policy="open", dm_allow_from=["alice"], group_policy="pairing", group_allow_from=[]
    )
    assert policy.is_dm_allowed("alice") is True
    assert policy.is_dm_allowed("stranger") is False


def test_wecom_open_gate_ignores_default_env_under_scope(profile_scope, multiplex_on, monkeypatch):
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

    from plugins.platforms.wecom.adapter import WeComAdapter

    adapter = WeComAdapter.__new__(WeComAdapter)
    adapter._dm_policy, adapter._group_policy = "open", "open"
    adapter._allow_from, adapter._group_allow_from = ["wecom-carol"], ["room-1"]
    adapter._groups = {}
    assert adapter._is_dm_allowed("wecom-carol") is True
    assert adapter._is_dm_allowed("stranger") is False
    assert adapter._is_group_allowed("room-1", "wecom-carol") is True
    assert adapter._is_group_allowed("room-2", "stranger") is False


def test_startup_never_refuses_open_policy_under_any_scope(multiplex_on, monkeypatch):
    """There is no open-policy startup guard anymore (#93522's scope question is
    moot): ``_start_check_access_policy`` returns False whether the (dead)
    ``GATEWAY_ALLOW_ALL_USERS`` flag sits in the active scope or only in
    os.environ."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner

    cfg = GatewayConfig(
        platforms={Platform.YUANBAO: PlatformConfig(enabled=True, extra={"dm_policy": "open"})}
    )
    runner = object.__new__(GatewayRunner)
    runner.config = cfg
    with _scope({"GATEWAY_ALLOW_ALL_USERS": "true"}):
        assert runner._start_check_access_policy() is False
    with _scope({}):
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        assert runner._start_check_access_policy() is False
