"""Behaviour contracts for /allowgc at the runner authorization gate.

The Telegram adapter auto-admits a group member into the GROUP store and
takes the normal turn path (see
``tests/plugins/platforms/telegram/test_allowgc_adapter.py``), but the turn
still has to pass the runner's ingress gate
(``GatewayRunner._hm_admit_event`` -> ``_is_user_authorized_for_source``),
which historically read only the LAUNCH profile's env allowlist. A member
who never started the bot in DM is on no launch allowlist, so the runner
dropped every trigger-word invocation and member command from them even
though the adapter had already admitted them. The runner gate must read the
GROUP store for allowgc sources instead.

All state lives under a temp HERMES_HOME — never the real one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.mongo_tenants import Principal
from gateway.allowgc import create_group_profile, opt_out
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.mongo_multiuser import store_for_home
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.profile_routing import parse_profile_routes
from gateway.session import SessionSource
from gateway.session_identity import resolve_identity

OWNER = "100"
MEMBER = "201"
STRANGER = "202"
GROUP = "-100123"
OTHER_GROUP = "-100456"


class _Stub(BasePlatformAdapter):
    pass


_Stub.__abstractmethods__ = frozenset()


def _stub(platform, runner, label):
    adapter = _Stub.__new__(_Stub)
    adapter.platform, adapter.gateway_runner, adapter.label = platform, runner, label
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._pending_messages, adapter._active_sessions = {}, {}
    return adapter


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Bare multiplexed GatewayRunner; launch allowlist holds the owner only.

    Mirrors tests/gateway/test_multiplex_routing_authz.py: the scoped env
    reads go through the launch home's .env, so a member admitted only in
    the group store is invisible to the launch allowlist.
    """
    from agent import secret_scope
    from gateway.run import GatewayRunner

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
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
    patches = [
        patch("hermes_cli.profiles.profiles_to_serve", return_value=served),
    ]
    for entered in patches:
        entered.start()
    try:
        yield SimpleNamespace(runner=runner, home=home, primary=primary)
    finally:
        for entered in patches:
            entered.stop()
        secret_scope.set_multiplex_active(prev)


def _group_source(runner, primary, chat_id=GROUP, user_id=MEMBER):
    """A group source exactly as the base adapter canonicalizes it at ingress."""
    source = primary.build_source(chat_id=chat_id, chat_type="group", user_id=user_id)
    resolve_identity(source, runner=runner)
    return source


def _admit_member(gc_home, user_id=MEMBER):
    """The adapter's auto-admission grant, group scope only, no sudo."""
    store_for_home(gc_home).grant_scope_access(Principal(OWNER), user_id, GROUP)


def test_runner_admits_auto_admitted_member_never_started_dm(rig):
    """(b) A trigger-word invocation from an auto-admitted member who never
    started the bot in DM reaches the runner gate: the group store, not the
    launch allowlist, decides."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is True


def test_runner_admits_owner_in_allowgc_group(rig):
    create_group_profile(GROUP, "Group", OWNER)
    source = _group_source(rig.runner, rig.primary, user_id=OWNER)

    assert rig.runner._is_user_authorized_for_source(source) is True


def test_runner_denies_never_admitted_member_in_allowgc_group(rig):
    """No group-store grant (never auto-admitted): still silent, even though
    allowgc is on for the chat."""
    create_group_profile(GROUP, "Group", OWNER)
    source = _group_source(rig.runner, rig.primary, user_id=STRANGER)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_member_in_non_allowgc_group(rig):
    """(f) No group profile: the launch allowlist alone decides, as before."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    source = _group_source(rig.runner, rig.primary, chat_id=OTHER_GROUP)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_opted_out_member(rig):
    """(e) /imout opt-out stays a silent ignore at the runner gate too."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    opt_out(gc_home, MEMBER)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_group_banned_member(rig):
    """A ban inside the group profile stays total at the runner gate."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    store_for_home(gc_home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_launch_banned_member_with_group_grant(rig):
    """A global (launch-store) ban stays total even with a group grant made
    before the ban: the group store alone must never override it."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    store_for_home(rig.home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_member_when_allowgc_disabled(rig):
    """A disabled group profile falls back to the launch allowlist."""
    from gateway.allowgc import disable_group_profile

    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    disable_group_profile(GROUP)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_admits_member_command_source(rig):
    """(c) Member commands take the same gate as trigger-word turns: an
    auto-admitted member passes it, so the command reaches the adapter."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized(source) is True
    assert rig.runner._is_user_authorized_for_source(source) is True


def test_auto_admit_admits_when_adapter_runs_under_launch_home(rig):
    """The adapter runs under the LAUNCH home in production (authorization
    stays with the receiving bot; see ``session_identity``), so a resolver
    that answers the launch home for the group source is the live topology,
    not a misroute. Auto-admission must grant in the GROUP store anyway —
    the old home-equality refusal denied every non-owner member, always.
    The grant must land in the group store only, never the launch store."""
    from gateway.config import PlatformConfig as _PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    gc_home = create_group_profile(GROUP, "Group", OWNER)
    adapter = TelethonAdapter(_PlatformConfig(extra={"require_mention": True}))
    # Resolver answers the LAUNCH home for the group source: production
    # authorization routing, where the group profile is a routed satellite.
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _src: rig.home,
    )
    source = SimpleNamespace(
        platform="telegram", chat_id=GROUP, chat_type="group", user_id=MEMBER,
        is_bot=False,
    )
    store = store_for_home(rig.home)
    owner = store.owner_user_id
    access = store.access_for_scope(MEMBER, "group", GROUP)
    assert access.get("allowed") is False

    effective = adapter._allowgc_auto_admit(source, owner, MEMBER, access)

    assert effective.get("allowed") is True
    assert effective.get("sudo") is False
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is True
    assert store_for_home(rig.home).get_access(MEMBER).get("allowed") is False


def test_opt_out_revokes_the_live_group_grant(rig):
    """The opt-out removal is wired, not just listed: opting out revokes
    the member's grant in the group store, so every store-only admission
    reader sees them as not allowed afterwards."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is True

    opt_out(gc_home, MEMBER)

    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False
    assert store_for_home(gc_home).get_access(MEMBER).get("allowed") is False
    # The launch store is untouched: the member was never a launch user.
    assert store_for_home(rig.home).get_access(MEMBER).get("allowed") is False


def test_opted_out_member_is_never_re_auto_admitted(rig):
    """An opted-out member with a grant from before the opt-out is not
    re-admitted on their next group message: auto-admission consults the
    opt-out list BEFORE its already-allowed shortcut and denies."""
    from gateway.config import PlatformConfig as _PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    opt_out(gc_home, MEMBER)
    adapter = TelethonAdapter(_PlatformConfig(extra={"require_mention": True}))
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _src: gc_home,
    )
    source = SimpleNamespace(
        platform="telegram", chat_id=GROUP, chat_type="group", user_id=MEMBER,
        is_bot=False,
    )
    group_store = store_for_home(gc_home)
    # Even if a grant somehow survived (e.g. recorded before the revoke
    # landed), the next message must not re-admit them.
    group_store.grant_scope_access(Principal(OWNER), MEMBER, GROUP)
    access = group_store.access_for_scope(MEMBER, "group", GROUP)
    assert access.get("allowed") is True

    effective = adapter._allowgc_auto_admit(
        source, OWNER, MEMBER, access)

    assert effective.get("allowed") is False
    assert effective.get("sudo") is False
    assert rig.runner._is_user_authorized_for_source(
        _group_source(rig.runner, rig.primary)) is False


def test_runner_gate_denials_are_logged_with_actor_and_chat(rig, caplog):
    """The runner gate's allowgc denials leave log lines with actor + chat
    id: an opted-out member, and a member with no group grant at all."""
    import logging

    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    opt_out(gc_home, MEMBER)
    opted_out = _group_source(rig.runner, rig.primary)
    with caplog.at_level(logging.INFO):
        assert rig.runner._is_user_authorized_for_source(opted_out) is False
    assert "opted out" in caplog.text
    assert MEMBER in caplog.text and GROUP in caplog.text

    caplog.clear()
    stranger = _group_source(rig.runner, rig.primary, user_id=STRANGER)
    with caplog.at_level(logging.INFO):
        assert rig.runner._is_user_authorized_for_source(stranger) is False
    assert "no group grant" in caplog.text
    assert STRANGER in caplog.text and GROUP in caplog.text


def _allow_globally(rig, user_id=MEMBER):
    """The /allow grant: a GLOBAL ``allowed: True`` flag on the launch
    profile — it names no scope and no group."""
    store_for_home(rig.home).allow_user(Principal(OWNER), user_id, True)


def test_runner_admits_dual_allowed_user_via_launch_grant(rig, caplog):
    """The hybrid gap (2026-11-10): a user granted globally by /allow who
    is also an allowgc-group member — with NO group-store row — is
    admitted at the runner gate via the launch grant, and the admission
    is logged as a launch-grant admission, never as 'no group grant'."""
    import logging

    create_group_profile(GROUP, "Group", OWNER)
    _allow_globally(rig)
    assert store_for_home(rig.home).get_access(MEMBER)["allowed"] is True
    source = _group_source(rig.runner, rig.primary)

    with caplog.at_level(logging.INFO):
        assert rig.runner._is_user_authorized_for_source(source) is True

    assert "admitted via launch grant" in caplog.text
    assert MEMBER in caplog.text and GROUP in caplog.text
    assert "no group grant" not in caplog.text


def test_runner_denies_opted_out_dual_allowed_user(rig):
    """A launch-allowed user who opted OUT of the group stays denied
    there: the opt-out check runs before the launch fallback, so the
    global grant never overrides a group opt-out."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _allow_globally(rig)
    opt_out(gc_home, MEMBER)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_globally_banned_dual_allowed_user(rig):
    """A launch-allowed user banned globally stays denied: the fallback
    falls through to the global-ban check, which is total."""
    create_group_profile(GROUP, "Group", OWNER)
    _allow_globally(rig)
    store_for_home(rig.home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_denies_group_banned_dual_allowed_user(rig):
    """A launch-allowed user banned inside the group stays denied there:
    the group ban returns False before the launch fallback is reached."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _allow_globally(rig)
    store_for_home(gc_home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(rig.runner, rig.primary)

    assert rig.runner._is_user_authorized_for_source(source) is False


def test_runner_admits_pure_group_member_without_launch_grant(rig, caplog):
    """A member with only a group-store grant (no launch grant) is
    admitted exactly as before, and the launch fallback does not fire
    for them — the union admits, it does not re-label."""
    import logging

    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    assert store_for_home(rig.home).get_access(MEMBER)["allowed"] is False
    source = _group_source(rig.runner, rig.primary)

    with caplog.at_level(logging.INFO):
        assert rig.runner._is_user_authorized_for_source(source) is True

    assert "admitted via launch grant" not in caplog.text


def test_group_grant_does_not_admit_in_dm(rig):
    """The reverse direction stays closed: a group-store grant never
    admits its holder in DM — group grants stay group-scoped, and the
    launch fallback only ever reads the GLOBAL launch flag."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    _admit_member(gc_home)
    assert store_for_home(rig.home).get_access(MEMBER)["allowed"] is False
    dm_source = rig.primary.build_source(
        chat_id=MEMBER, chat_type="dm", user_id=MEMBER)
    resolve_identity(dm_source, runner=rig.runner)

    assert rig.runner._is_user_authorized_for_source(dm_source) is False
