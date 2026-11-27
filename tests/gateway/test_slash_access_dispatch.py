"""Integration tests for slash command access control gating in gateway/run.py.

Drives the real ``GatewayRunner._handle_message`` path with a stub session
store so we exercise the actual gate inserted at the dispatch site (not a
re-implementation in the test). Uses the same ``object.__new__`` runner
construction pattern as test_status_command.py.

Coverage targets (no-limits: any admitted user may run any slash command;
admission itself is unchanged and checked before dispatch):
  - Backward compat: no ``allow_admin_from`` set → behaves exactly as before
    (no denial messages, dispatch reaches the real handler).
  - Admin path: user in ``allow_admin_from`` runs anything.
  - User path: user not in admin list runs anything too; the admin/user
    command lists no longer gate ``can_run``.
  - Always-allowed floor: /help and /whoami reachable for non-admins
    even with empty user_allowed_commands.
  - DM vs group scope isolation (policy resolution still per scope).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.session_identity import RoutingIdentity


def _make_source(
    *,
    platform: Platform = Platform.DISCORD,
    user_id: str = "user1",
    chat_type: str = "dm",
    chat_id: str = "c1",
    profile: str | None = None,
) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name=f"name-{user_id}",
        chat_type=chat_type,
        profile=profile,
    )


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_runner(*, platform_extra: dict | None = None,
                 platform: Platform = Platform.DISCORD,
                 multiplex_profiles: bool = False):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=multiplex_profiles,
        platforms={
            platform: PlatformConfig(
                enabled=True,
                token="***",
                extra=platform_extra or {},
            )
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    session_entry = SessionEntry(
        session_key="agent:main:discord:dm:c1",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._primary_profile_name = "primary"
    runner._profile_configs = {}
    runner._pending_messages = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


# ---------------------------------------------------------------------------
# /whoami response shape — proves the handler is reachable AND uses the
# resolver. We use /whoami because it's deterministic and short-circuits
# before any session/agent setup.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_non_admin_lists_runnable_commands():
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": ["status", "model"],
        }
    )
    result = await runner._handle_message(_make_event("/whoami", _make_source(user_id="999")))
    assert "Tier: user" in result
    assert "/help" in result      # always-allowed floor
    assert "/whoami" in result    # always-allowed floor
    assert "/status" in result
    assert "/model" in result


@pytest.mark.asyncio
async def test_secondary_profile_slash_policy_uses_its_own_config():
    """#121705: a bot configured only in a secondary profile is gated by THAT profile's
    ``allow_admin_from``; the launch profile's empty policy must not make it unrestricted.
    No-limits: the per-bot policy still resolves (tier + is_admin), but the gate no
    longer denies any admitted user — ``_check_slash_access`` returns None for both
    the beta user and the beta admin."""
    from gateway.slash_access import policy_for_runner_source

    runner = _make_runner(multiplex_profiles=True)
    runner._profile_configs["beta"] = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={"allow_admin_from": ["admin"], "user_allowed_commands": ["status"]},
            )
        }
    )
    user = _make_source(user_id="user", profile="beta")
    # Beta's own config resolves: gating enabled, "admin" is admin, "user" is not.
    beta_policy = policy_for_runner_source(runner, user)
    assert beta_policy.enabled is True
    assert beta_policy.is_admin("admin") is True
    assert beta_policy.is_admin("user") is False
    # ... but no admitted user is denied any slash command anymore.
    assert runner._check_slash_access(user, "restart") is None
    assert "Tier: user" in await runner._handle_whoami_command(_make_event("/whoami", user))
    assert runner._resume_caller_is_admin(user) is False

    admin = _make_source(user_id="admin", profile="beta")
    assert runner._check_slash_access(admin, "restart") is None
    assert runner._resume_caller_is_admin(admin) is True

    # The launch profile's own (ungated) bot stays ungated ...
    assert runner._check_slash_access(_make_source(user_id="user"), "restart") is None
    # ... including a turn ROUTED to beta: ``allow_admin_from`` is per bot, so the bot-owning
    # (transport) profile's config decides, the owner ``authorization_home`` already names.
    routed = _make_source(user_id="user", profile="beta")
    routed._identity = RoutingIdentity(
        transport_profile="primary", runtime_profile="beta",
        authorization_home=Path("/profiles/primary"), runtime_home=Path("/profiles/beta"),
    )
    assert runner._check_slash_access(routed, "restart") is None


def test_missing_secondary_profile_config_fails_closed():
    """A served profile whose config is not cached must not inherit the launch profile's open policy.
    No-limits: the fail-closed POLICY still resolves (enabled, empty admin list — visible as
    "Tier: user" in /whoami, never "Tier: unrestricted"), but the slash gate no longer denies
    any admitted user, so /restart is no longer refused at the gate. If the slash denial for
    uncached-profile users is load-bearing, that is a no-limits regression to rule on — the
    policy resolution itself is what this test pins."""
    from gateway.slash_access import policy_for_runner_source

    runner = _make_runner(multiplex_profiles=True)
    source = _make_source(user_id="user", profile="beta")
    policy = policy_for_runner_source(runner, source)
    assert policy.enabled is True  # fail-closed, NOT the launch profile's open policy
    assert policy.admin_user_ids == frozenset()
    assert policy.is_admin("user") is False
    # No-limits: the gate admits any admitted user even under the fail-closed policy.
    assert runner._check_slash_access(source, "restart") is None
    assert runner._check_slash_access(source, "help") is None


@pytest.mark.asyncio
async def test_help_non_admin_lists_only_runnable_commands():
    """/help for a gated non-admin renders the floor + user_allowed_commands, never the
    admin-only catalog the dispatcher would then refuse; admins keep the full list."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": ["status"],
        }
    )
    user = await runner._handle_message(_make_event("/help", _make_source(user_id="999")))
    assert "`/help" in user and "`/whoami" in user and "`/status" in user
    assert "`/model" not in user and "`/restart" not in user
    admin = await runner._handle_message(_make_event("/help", _make_source(user_id="111")))
    assert "`/model" in admin and "`/restart" in admin


# ---------------------------------------------------------------------------
# No-limits gate — a non-admin is no longer denied any slash command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_with_empty_user_commands_runs_any_command():
    """No-limits: a non-admin with an explicitly empty ``user_allowed_commands``
    list is no longer denied anything — /stop reaches its handler (no agent
    running, so it reports that) and /whoami still renders the user tier."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": [],  # explicitly empty
        }
    )
    # /stop dispatches instead of being denied
    result = await runner._handle_message(_make_event("/stop", _make_source(user_id="999")))
    assert "⛔" not in (result or "")
    assert result == "No active task to stop."
    # /whoami still works (always-allowed floor)
    whoami_result = await runner._handle_message(_make_event("/whoami", _make_source(user_id="999")))
    assert "Tier: user" in whoami_result


def test_explicitly_allowed_telegram_account_is_effective_slash_admin():
    runner = _make_runner(
        platform=Platform.TELEGRAM,
        platform_extra={"allow_admin_from": ["owner"], "user_allowed_commands": []},
    )

    class TelegramAdapter:
        def _is_explicitly_allowed(self, source):
            return source.user_id == "granted"

    runner.adapters[Platform.TELEGRAM] = TelegramAdapter()
    source = _make_source(platform=Platform.TELEGRAM, user_id="granted")
    assert runner._check_slash_access(source, "restart") is None
    assert runner._telegram_explicitly_allowed(source) is True


# ---------------------------------------------------------------------------
# Gate ALLOW — admin and listed user
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backward compatibility — no admin list set means no gating at all
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scope isolation — DM vs group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_only_gating_leaves_dm_unrestricted():
    runner = _make_runner(
        platform_extra={
            # Only group has an admin list → DM scope stays in backward-compat mode
            "group_allow_admin_from": ["222"],
        }
    )
    result = await runner._handle_message(_make_event("/whoami", _make_source(user_id="anyone", chat_type="dm")))
    assert "Tier: unrestricted" in result


@pytest.mark.asyncio
async def test_blank_chat_type_resolves_to_gated_scope():
    """A blank chat_type (relay frames can send ""/null; restored rows keep a
    stored empty value) used to resolve to group scope, so on a DM-only-gated
    install the source landed in an ungated scope and every command ran.
    No-limits: the gate no longer denies, so the #116282 scope resolution is
    pinned via /whoami instead — a blank source under DM-only gating must
    still resolve to the gated DM scope ("Tier: user"), never to the ungated
    group scope ("Tier: unrestricted") — and /stop must reach its handler."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": ["status"],
        }
    )
    for blank in ("", None):
        result = await runner._handle_message(
            _make_event("/stop", _make_source(user_id="999", chat_type=blank))
        )
        assert "⛔" not in (result or ""), repr(blank)
        assert result == "No active task to stop.", repr(blank)
        whoami = await runner._handle_message(
            _make_event("/whoami", _make_source(user_id="999", chat_type=blank))
        )
        assert "Tier: user" in whoami, repr(blank)
        assert "Tier: unrestricted" not in whoami, repr(blank)


# ---------------------------------------------------------------------------
# Plugin-registered slash commands are gated through the same path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_runs_unlisted_quick_command_exec():
    """No-limits: a non-admin may now reach the quick_commands exec sink for a
    command that isn't in user_allowed_commands. Regression for #44727 — quick
    commands are never in the gateway registry, so the early gate skips them;
    the sink gate must still be the one that decides — and under no-limits it
    admits any admitted user, so the exec output is returned."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": [],
        }
    )
    runner.config.quick_commands = {
        "limits": {"type": "exec", "command": "printf quick-command-bypass-confirmed"}
    }

    result = await runner._handle_message(
        _make_event("/limits", _make_source(user_id="999"))
    )

    assert result is not None
    assert "⛔" not in result
    assert result == "quick-command-bypass-confirmed"


@pytest.mark.asyncio
async def test_admin_runs_quick_command_when_gating_enabled():
    """An admin runs the quick command even under an enabled gate with an
    empty user_allowed_commands list."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": [],
        }
    )
    runner.config.quick_commands = {
        "limits": {"type": "exec", "command": "printf quick-command-admin"}
    }

    result = await runner._handle_message(
        _make_event("/limits", _make_source(user_id="111"))
    )

    assert result == "quick-command-admin"


# ---------------------------------------------------------------------------
# Running-agent fast-path gating — admin/user split must hold even when an
# agent is already running. The fast-path block in _handle_message dispatches
# /stop, /restart, /new, /steer, /model, /approve, /deny, /agents,
# /bg, /btw, /kanban, /goal, /yolo, /verbose, /footer, /help,
# /profile, /update directly without going through the cold dispatch site.
# We must apply the gate there too — otherwise non-admins could bypass
# gating just because an agent happens to be busy.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_agent_fastpath_allows_admin_command():
    """Admins must still be able to run privileged commands like /restart
    through the running-agent fast-path. We check that we don't get the
    denial message; the actual /restart handler is mocked out via the
    runner's MagicMock."""
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["111"],
            "user_allowed_commands": [],
        }
    )
    src = _make_source(user_id="111")  # admin
    sk = build_session_key(src)
    runner._running_agents[sk] = MagicMock()
    runner._session_state(sk).turn.started_ts = 0
    # Mock the restart handler so it doesn't actually try to restart anything.
    runner._handle_restart_command = AsyncMock(return_value="restart-handled")

    result = await runner._handle_message(_make_event("/restart", src))
    assert result == "restart-handled"
    assert "⛔" not in (result or "")


# ---------------------------------------------------------------------------
# Alias resolution — /h aliases to /help; the gate must canonicalize before
# checking access. /hist (history alias) is a real one to exercise.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unknown / unregistered command — gate must NOT intercept (let the existing
# unknown-command path handle it normally).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scope independence — admin in DM scope is NOT auto-admin in group when
# group has its own admin list (regression guard for the "admin lists are
# scope-specific" rule).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-platform isolation — gating on Discord doesn't leak to Telegram.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gating_isolated_per_platform():
    """When Discord is gated and Telegram isn't, the same user_id on
    Telegram must be unrestricted."""
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform, PlatformConfig

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="***",
                extra={
                    "allow_admin_from": ["111"],
                    "user_allowed_commands": [],
                },
            ),
            Platform.TELEGRAM: PlatformConfig(
                enabled=True, token="***", extra={}
            ),
        }
    )
    runner.adapters = {
        Platform.DISCORD: MagicMock(send=AsyncMock()),
        Platform.TELEGRAM: MagicMock(send=AsyncMock()),
    }
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    session_entry = SessionEntry(
        session_key="agent:main:telegram:dm:c1",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._emit_gateway_run_progress = AsyncMock()

    # Same user_id on Telegram → must be unrestricted (Telegram has no admin list).
    tg_src = _make_source(platform=Platform.TELEGRAM, user_id="999", chat_id="t1")
    result = await runner._handle_message(_make_event("/whoami", tg_src))
    assert "Tier: unrestricted" in result
