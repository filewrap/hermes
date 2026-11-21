"""Behaviour contracts for /allowgc auto-admission under production routing.

The live failure (2026-11-09, Testing Agents): every non-owner member's
message died silently in the adapter with "the group store is not the
active store". The cause was the topology, not the members: the adapter
runs under the LAUNCH home — ``resolve_identity`` pins
``authorization_home`` to the receiving (launch) bot's home while the
group turn runs in the ``gc-<chat_id>`` profile — so the old
home-equality guard in ``_allowgc_auto_admit`` refused every member,
always, and only the owner (who bypasses via the early allowed return)
ever got a reply.

These tests reproduce that topology instead of the old fixture's
assumption: a real bare multiplexed ``GatewayRunner`` and real
``resolve_identity`` pin the identity, so ``_multiuser_home`` returns
the LAUNCH home for a group source while the group profile is the gc
home. A test that passes only because the two homes are mocked equal is
the bug repeating itself — see ``test_multiuser_home_is_launch_home``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telethon import types

from agent.mongo_tenants import Principal
from gateway.allowgc import create_group_profile, group_home_for_source, is_active_for_chat, opt_out, resolve_home
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.mongo_multiuser import store_for_home
from gateway.pairing import PairingStore
from gateway.platforms.base import BasePlatformAdapter
from gateway.profile_routing import parse_profile_routes
from gateway.session_identity import resolve_identity
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

OWNER = "100"
MEMBER = "201"
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
def transport(tmp_path, monkeypatch):
    """Real multiplexed runner + real identity; adapter under the launch home.

    Mirrors ``tests/gateway/test_allowgc_runner_authz.py::rig``: the launch
    allowlist holds the owner only, and a member admitted only in the group
    store is invisible to it. The adapter's ``gateway_runner`` is the real
    bare ``GatewayRunner`` — its ``_authorization_home_for_source`` (via the
    pinned identity) answers the LAUNCH home for group sources, exactly as
    ``gateway/session_identity.py::resolve_identity`` pins it in
    production, while ``_resolve_profile_home_for_source`` answers the gc
    home for the turn itself.
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
    entered = patch("hermes_cli.profiles.profiles_to_serve", return_value=served)
    entered.start()

    adapter = TelethonAdapter(PlatformConfig(extra={"require_mention": True}))
    adapter.gateway_runner = runner
    adapter._bot_username = "NainaAssistantBot"
    adapter._bot_name = "Naina"
    adapter.send_reaction = AsyncMock(return_value=True)
    adapter._source = AsyncMock(side_effect=lambda event: event.source)
    adapter._cache_inbound_peer = AsyncMock()
    adapter._send_rich_ui = AsyncMock(return_value=SimpleNamespace(id=55))
    adapter._edit_rich_ui_message = AsyncMock()
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._input_peer_for = AsyncMock(
        side_effect=lambda uid: types.InputPeerUser(int(uid), 98765))
    adapter._client = AsyncMock(return_value=SimpleNamespace(id=901))
    adapter._client.get_entity = AsyncMock()
    adapter._client.get_input_entity = AsyncMock(
        side_effect=lambda peer: types.InputUser(int(getattr(peer, "user_id", peer)), 98765))
    adapter._peer_for = lambda chat_id: types.InputPeerSelf()

    tenant_handler = AsyncMock(return_value={"final_response": "tenant reply"})
    entered_tenant = patch(
        "gateway.tenant_runtime.handle_tenant_message", tenant_handler)
    entered_tenant.start()
    entered_react = patch(
        "plugins.platforms.telegram.incoming_reactions.react_to_incoming",
        AsyncMock())
    entered_react.start()
    try:
        yield SimpleNamespace(
            adapter=adapter, runner=runner, primary=primary, home=home,
            tenant_handler=tenant_handler,
        )
    finally:
        entered.stop()
        entered_tenant.stop()
        entered_react.stop()
        secret_scope.set_multiplex_active(prev)


def _group_source(transport, user_id=MEMBER, *, chat_id=GROUP, is_bot=False):
    """A group source exactly as ingress canonicalizes it: real identity pinned."""
    source = transport.primary.build_source(
        chat_id=str(chat_id), chat_type="group", user_id=str(user_id), is_bot=is_bot)
    resolve_identity(source, runner=transport.runner)
    return source


def _message(text, source, *, message_id=55):
    return SimpleNamespace(
        source=source,
        message=SimpleNamespace(message=text, id=message_id, reply_to_msg_id=None),
        get_sender=AsyncMock(return_value=SimpleNamespace(
            id=int(source.user_id),
            username=f"user_{source.user_id}",
            first_name=f"User {source.user_id}",
            bot=getattr(source, "is_bot", False),
        )),
    )


async def _send(transport, text, source, *, message_id=55):
    await transport.adapter._on_new_message(_message(text, source, message_id=message_id))


def test_multiuser_home_is_launch_home_for_allowgc_group(transport):
    """Pin the topology: authorization stays with the launch bot, the turn
    runs in the gc profile. If this ever reads the gc home, the test below
    would pass for the wrong reason (mocked-equal homes)."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    source = _group_source(transport)

    assert transport.adapter._multiuser_home(source) == transport.home
    assert group_home_for_source(source) == gc_home
    assert transport.adapter._multiuser_home(source) != gc_home


@pytest.mark.asyncio
async def test_never_started_member_first_message_admitted(transport):
    """The live bug, end to end: a never-started, non-owner member's first
    message in an active allowgc group is admitted, the grant lands in the
    GROUP store (never the launch store), and the runner gate then admits
    the same source, so the member takes the normal turn path."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    assert is_active_for_chat(GROUP)
    source = _group_source(transport)

    await _send(transport, "@NainaAssistantBot hello", source)

    group_store = store_for_home(gc_home)
    group_access = group_store.access_for_scope(MEMBER, "group", GROUP)
    assert group_access["allowed"] is True
    assert group_access["sudo"] is False
    assert group_store.get_access(MEMBER)["allowed"] is False
    assert store_for_home(transport.home).get_access(MEMBER)["allowed"] is False
    transport.tenant_handler.assert_not_awaited()
    transport.adapter.handle_message.assert_awaited_once()
    # Same source, same store: the runner gate admits the member too.
    assert transport.runner._is_user_authorized_for_source(source) is True


@pytest.mark.asyncio
async def test_auto_admit_writes_group_store_under_launch_home_routing(transport):
    """Direct contract on ``_allowgc_auto_admit``: with the adapter running
    under the launch home (production), the grant still lands in the group
    store and the effective access comes back from there."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    source = _group_source(transport)
    launch_store = store_for_home(transport.home)
    access = launch_store.access_for_scope(MEMBER, "group", GROUP)
    assert access.get("allowed") is False

    effective = transport.adapter._allowgc_auto_admit(
        source, OWNER, MEMBER, access)

    assert effective.get("allowed") is True
    assert effective.get("sudo") is False
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is True
    assert store_for_home(transport.home).get_access(MEMBER)["allowed"] is False


@pytest.mark.asyncio
async def test_owner_still_bypasses_without_group_grant_attempt(transport, caplog):
    """The owner keeps the early allowed return: his message is served and
    no auto-admission failure is logged for him (a grant attempt on the
    owner would raise 'Owner access cannot be changed')."""
    create_group_profile(GROUP, "Group", OWNER)
    source = _group_source(transport, user_id=OWNER)

    with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.telethon_adapter"):
        await _send(transport, "@NainaAssistantBot hello", source)

    transport.adapter.handle_message.assert_awaited_once()
    assert not [r for r in caplog.records if "auto-admission" in r.getMessage()]


@pytest.mark.asyncio
async def test_group_banned_member_still_refused_and_logged(transport, caplog):
    """A ban inside the group profile stays total; the refusal log names
    the refused member (the live diagnostic gap: the old warning carried
    no actor id). The refusal is an INFO skip now — the group-store
    access check short-circuits before any grant attempt — so the log is
    captured at INFO like the opt-out skip, not at WARNING."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    store_for_home(gc_home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)

    with caplog.at_level(logging.INFO):
        await _send(transport, "@NainaAssistantBot hello", source)

    transport.adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False
    assert any(MEMBER in r.getMessage() and GROUP in r.getMessage()
               for r in caplog.records if "auto-admission" in r.getMessage())


@pytest.mark.asyncio
async def test_globally_banned_member_still_refused(transport):
    """A global (launch-store) ban stays total even with an active group
    profile: the call site drops the message before auto-admission, and
    no grant lands in the group store."""
    create_group_profile(GROUP, "Group", OWNER)
    store_for_home(transport.home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)

    await _send(transport, "@NainaAssistantBot hello", source)

    transport.adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    assert store_for_home(resolve_home(GROUP, reap=False)).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False


@pytest.mark.asyncio
async def test_auto_admit_refuses_globally_banned_member_and_logs_actor(transport, caplog):
    """The auto-admission function's own global-ban refusal (defense in
    depth for a ban landing after the access read): the refusal log names
    the refused member and chat."""
    create_group_profile(GROUP, "Group", OWNER)
    store_for_home(transport.home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)
    # Stale, pre-ban access as the function received it: the launch-ban
    # check inside auto-admission must still refuse.
    access = {"allowed": False, "banned": False, "sudo": False, "started": False}

    with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.telethon_adapter"):
        effective = transport.adapter._allowgc_auto_admit(source, OWNER, MEMBER, access)

    assert effective.get("allowed") is False
    assert store_for_home(resolve_home(GROUP, reap=False)).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False
    assert any(MEMBER in r.getMessage() and GROUP in r.getMessage()
               for r in caplog.records if "auto-admission" in r.getMessage())


@pytest.mark.asyncio
async def test_opted_out_member_still_refused(transport):
    """An opted-out member is never re-admitted, even with an active group."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    opt_out(gc_home, MEMBER)
    source = _group_source(transport)

    await _send(transport, "@NainaAssistantBot hello", source)

    transport.adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False


@pytest.mark.asyncio
async def test_bot_member_still_refused(transport):
    """Bot-authored messages never take the auto-admission path."""
    create_group_profile(GROUP, "Group", OWNER)
    source = _group_source(transport, is_bot=True)

    await _send(transport, "@NainaAssistantBot hello", source)

    transport.adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    assert store_for_home(resolve_home(GROUP, reap=False)).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False


@pytest.mark.asyncio
async def test_inactive_group_still_refuses(transport):
    """No allowgc profile for the chat: the member is not admitted and
    takes no turn."""
    source = _group_source(transport, chat_id=OTHER_GROUP)

    await _send(transport, "@NainaAssistantBot hello", source)

    transport.adapter.handle_message.assert_not_awaited()
    transport.tenant_handler.assert_not_awaited()
    assert store_for_home(transport.home).get_access(MEMBER)["allowed"] is False


@pytest.mark.asyncio
async def test_opted_out_auto_admission_skip_is_logged(transport, caplog):
    """An opted-out member's silent auto-admission skip leaves a log line
    with the actor and chat id — the 948247711 case must be identifiable."""
    import logging

    gc_home = create_group_profile(GROUP, "Group", OWNER)
    opt_out(gc_home, MEMBER)
    source = _group_source(transport)
    launch_store = store_for_home(transport.home)
    access = launch_store.access_for_scope(MEMBER, "group", GROUP)
    assert access.get("allowed") is False

    with caplog.at_level(logging.INFO):
        effective = transport.adapter._allowgc_auto_admit(
            source, OWNER, MEMBER, access)

    assert effective.get("allowed") is False
    assert "opted out" in caplog.text
    assert MEMBER in caplog.text and GROUP in caplog.text


@pytest.mark.asyncio
async def test_dual_allowed_user_gets_group_grant_written(transport):
    """The hybrid gap (2026-11-10): a user granted globally by /allow
    (launch-store ``allowed: True``) who is also an allowgc-group member
    must still get a GROUP-store grant. The old already-allowed shortcut
    read the LAUNCH access and returned without ever writing the group
    row, so the runner gate — which reads only the group store — denied
    every group message from them silently. The grant stays group-scoped:
    no sudo, no global flag in the group store."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    launch_store = store_for_home(transport.home)
    launch_store.allow_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)
    access = launch_store.access_for_scope(MEMBER, "group", GROUP)
    assert access.get("allowed") is True

    effective = transport.adapter._allowgc_auto_admit(
        source, OWNER, MEMBER, access)

    assert effective.get("allowed") is True
    assert effective.get("sudo") is False
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is True
    assert store_for_home(gc_home).get_access(MEMBER)["allowed"] is False


@pytest.mark.asyncio
async def test_dual_allowed_user_first_message_admitted_end_to_end(transport):
    """Same hybrid gap, end to end: the dual user's first group message
    is admitted, the group-store grant is written, and the runner gate
    then admits the same source, so the member takes the normal turn
    path instead of being silently ignored."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    store_for_home(transport.home).allow_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)

    await _send(transport, "@NainaAssistantBot hello", source)

    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is True
    transport.adapter.handle_message.assert_awaited_once()
    assert transport.runner._is_user_authorized_for_source(source) is True


@pytest.mark.asyncio
async def test_dual_allowed_opted_out_user_still_denied(transport):
    """A launch-allowed user who opted OUT of the group stays denied
    there: the group opt-out is total in that group and the launch grant
    never overrides it."""
    gc_home = create_group_profile(GROUP, "Group", OWNER)
    opt_out(gc_home, MEMBER)
    launch_store = store_for_home(transport.home)
    launch_store.allow_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)
    access = launch_store.access_for_scope(MEMBER, "group", GROUP)
    assert access.get("allowed") is True

    effective = transport.adapter._allowgc_auto_admit(
        source, OWNER, MEMBER, access)

    assert effective.get("allowed") is False
    assert effective.get("sudo") is False
    assert store_for_home(gc_home).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False


@pytest.mark.asyncio
async def test_dual_allowed_globally_banned_user_denied(transport):
    """A launch-allowed user banned globally after the access read is
    still refused by auto-admission: the global-ban check must run before
    any already-allowed shortcut and must return denied access, not the
    stale allowed dict."""
    create_group_profile(GROUP, "Group", OWNER)
    store_for_home(transport.home).allow_user(Principal(OWNER), MEMBER, True)
    store_for_home(transport.home).ban_user(Principal(OWNER), MEMBER, True)
    source = _group_source(transport)
    # Stale, pre-ban access as the function received it: launch-allowed.
    access = {"allowed": True, "banned": False, "sudo": False, "started": False}

    effective = transport.adapter._allowgc_auto_admit(
        source, OWNER, MEMBER, access)

    assert effective.get("allowed") is False
    assert store_for_home(resolve_home(GROUP, reap=False)).access_for_scope(
        MEMBER, "group", GROUP).get("allowed") is False
