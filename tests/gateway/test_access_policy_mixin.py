"""Every own-policy adapter must reach the same allow/deny verdict for the same policy inputs.

The predicates used to be five hand-copied bodies (weixin, wecom, qqbot, whatsapp, yuanbao) that
drifted on blank principals and scoped env reads; ``OwnAccessPolicyMixin`` is the single rule.

Access contract: there is no allow-all switch. A platform configured ``"open"`` behaves as
``"allowlist"`` — it admits exactly the senders/chats the allowlist admits, never everyone —
whether or not any ``*_ALLOW_ALL_USERS`` / ``GATEWAY_ALLOW_ALL_USERS`` env var is set.
"""

from __future__ import annotations

import itertools

import pytest

from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin

POLICIES = ("open", "allowlist", "disabled", "pairing", "typo")
SENDERS = ("alice", "stranger", "", "   ", None)


def _hosts():
    """One bare instance per own-policy class, attributes set exactly as the adapters do."""
    from plugins.platforms.qqbot.adapter import QQAdapter
    from plugins.platforms.weixin.adapter import WeixinAdapter
    from plugins.platforms.whatsapp_cloud.adapter import WhatsAppCloudAdapter
    from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
    from plugins.platforms.yuanbao.adapter import AccessPolicy
    from plugins.platforms.wecom.adapter import WeComAdapter

    hosts = {
        "weixin": object.__new__(WeixinAdapter),
        "wecom": object.__new__(WeComAdapter),
        "qqbot": object.__new__(QQAdapter),
        "whatsapp": WhatsAppBehaviorMixin(),
        "whatsapp_cloud": object.__new__(WhatsAppCloudAdapter),
        "yuanbao": AccessPolicy("open", [], "open", []),
    }
    hosts["whatsapp"]._dm_allowlist_source = "config"
    hosts["whatsapp_cloud"]._dm_allowlist_source = "config"
    hosts["wecom"]._groups = {}
    return hosts


def _verdicts(host, name, dm_policy, group_policy):
    host._dm_policy, host._group_policy = dm_policy, group_policy
    host._allow_from, host._group_allow_from = ["alice"], ["room-1"]
    out = {}
    for sender in SENDERS:
        out[("dm", sender)] = host._is_dm_allowed(sender or "")
        out[("intake", sender)] = host._is_dm_intake_allowed(sender)
    for group in ("room-1", "room-2"):
        args = (group, "alice") if name in ("wecom", "qqbot") else (group,)
        out[("group", group)] = host._is_group_allowed(*args)
    return out


# Opt-in env vars that no longer grant anything. Setting them must not change
# any verdict — that is exactly what the fail-closed contract requires.
DEAD_OPT_IN = ("GATEWAY_ALLOW_ALL_USERS", "WEIXIN_ALLOW_ALL_USERS", "WECOM_ALLOW_ALL_USERS",
               "QQ_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS",
               "WHATSAPP_CLOUD_ALLOW_ALL_USERS", "YUANBAO_ALLOW_ALL_USERS")
# Hosts whose allowlists document ``*`` (weixin/yuanbao match literally, as before).
WILDCARD_HOSTS = ("wecom", "qqbot", "whatsapp", "whatsapp_cloud")


def _all_agree(hosts, label):
    for dm_policy, group_policy in itertools.product(POLICIES, POLICIES):
        table = {}
        for name, host in hosts.items():
            table[name] = _verdicts(host, name, dm_policy, group_policy)
        for name, verdicts in table.items():
            assert verdicts == table["weixin"], (name, dm_policy, group_policy, label)
        # "open" behaves as "allowlist": the allowlisted sender passes, strangers
        # never do — regardless of any allow-all env var. "pairing" still admits
        # on intake (the pairing handshake), "disabled"/unknown admit nobody.
        admitted = dm_policy in ("open", "allowlist")
        assert table["weixin"][("dm", "alice")] is admitted
        assert table["weixin"][("dm", "stranger")] is False
        assert table["weixin"][("intake", "   ")] is False  # blank principal never admitted
        assert table["weixin"][("intake", "stranger")] is (dm_policy == "pairing")
        assert table["weixin"][("intake", "alice")] is (dm_policy in ("open", "allowlist", "pairing"))
        group_admitted = group_policy in ("open", "allowlist")
        assert table["weixin"][("group", "room-1")] is group_admitted
        assert table["weixin"][("group", "room-2")] is False


@pytest.mark.parametrize("mode", ["none", "gateway", "platform", "all"])
def test_all_own_policy_adapters_agree(monkeypatch, mode):
    for var in DEAD_OPT_IN:
        monkeypatch.delenv(var, raising=False)
    if mode == "gateway":
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    elif mode == "platform":
        for var in DEAD_OPT_IN[1:]:
            monkeypatch.setenv(var, "true")
    elif mode == "all":
        for var in DEAD_OPT_IN:
            monkeypatch.setenv(var, "true")
    _all_agree(_hosts(), mode)


@pytest.mark.parametrize("name", WILDCARD_HOSTS)
def test_wildcard_allowlist_admits_strangers_on_every_path(name):
    """``*`` must open strict DM auth, DM intake AND group intake alike — whatsapp_cloud once
    honoured it on intake only, then on neither."""
    host = _hosts()[name]
    host._dm_policy = host._group_policy = "allowlist"
    host._allow_from, host._group_allow_from = ["*"], ["*"]
    group_args = ("room-2", "stranger") if name in ("wecom", "qqbot") else ("room-2",)
    assert host._is_dm_allowed("stranger") is True
    assert host._is_dm_intake_allowed("stranger") is True
    assert host._is_group_allowed(*group_args) is True
    assert host._is_dm_intake_allowed("   ") is False


def test_mixin_host_needs_no_allow_all_prefix():
    """The ``ALLOW_ALL_ENV_PREFIX`` class attribute is gone: a bare mixin host
    is constructed normally and its "open" policy fails closed to the allowlist."""

    class Host(OwnAccessPolicyMixin):
        _dm_policy, _group_policy, _allow_from, _group_allow_from = "open", "open", [], []

    assert Host()._is_dm_allowed("anyone") is False


def test_allow_all_env_names_are_gone_and_open_fails_closed(monkeypatch):
    """No ``_allow_all_env_names`` / opt-in concept remains: with policy "open"
    and every allow-all env var set, a stranger is still denied."""

    class Host(OwnAccessPolicyMixin):
        _dm_policy, _group_policy, _allow_from, _group_allow_from = "open", "open", ["alice"], ["room-1"]

    for var in DEAD_OPT_IN:
        monkeypatch.setenv(var, "true")
    host = Host()
    assert not hasattr(host, "_allow_all_env_names")
    assert host._is_dm_allowed("alice") is True
    assert host._is_dm_allowed("anyone") is False
    assert host._is_group_allowed("room-1") is True
    assert host._is_group_allowed("room-2") is False
