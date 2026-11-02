"""Phase 5 Group D: Telegram rich cards for /codex-runtime, /personality,
/verbose, /footer, /autoprune, /sethome, /voice, /platform, /restart,
/update, /debug, /login, /sync, /pause, /reload-mcp, /reload-skills,
/rollback, /diff.

Invariant tests, written red-first: on Telegram each handler sends one native
rich card through ``adapter.send_command_card`` and returns None (no plain-text
fallback); on other platforms the plain-text reply is unchanged.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _source(platform: Platform = Platform.TELEGRAM, chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester", chat_type=chat_type,
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM,
           chat_type: str = "dm") -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform, chat_type), message_id="m1")


def _make_runner(*, telegram_card: bool = True, platform: Platform = Platform.TELEGRAM):
    """Bare GatewayRunner with a fake Telegram adapter exposing send_command_card."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    card = AsyncMock(return_value=True) if telegram_card else None
    adapter = SimpleNamespace(send=AsyncMock(), send_command_card=card)
    runner.adapters = {platform: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._reply_anchor_for_event = lambda event: None
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:u1"
    runner._running_agents = {}
    runner._background_tasks = set()
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn: fn())
    runner._display_config_target = lambda event: ("/tmp/fake-config.yaml", "telegram")
    runner._voice_mode = {}
    runner._voice_key_for_source = lambda source: "telegram:c1"
    runner._get_guild_id = lambda event: None
    runner._failed_platforms = {}
    runner._restart_requested = False
    runner._draining = False
    runner._running_agent_count = lambda: 0
    runner._is_stale_restart_redelivery = lambda event: False
    runner._read_user_config = dict
    runner._execute_mcp_reload = AsyncMock(return_value="MCP reloaded.")
    runner._checkpoint_manager = lambda source: None
    runner._terminal_cwd = lambda: "/tmp"
    runner.config = None
    return runner, adapter


def _card_html(adapter) -> str:
    card = adapter.send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs["html"]


# --- Pure card builders -------------------------------------------------------

def test_run_command_payload_allowlists_config_commands():
    from plugins.platforms.telegram.command_cards import run_command_payload

    for cmd in ("codex-runtime", "personality", "busy", "autoprune",
                "platform", "rollback", "diff"):
        assert run_command_payload(cmd) == f"hm:run:/{cmd}"
    for mutating in ("verbose", "footer", "sethome", "voice", "restart",
                     "update", "debug", "login", "sync", "pause",
                     "reload-mcp", "reload-skills"):
        with pytest.raises(ValueError):
            run_command_payload(mutating)


def test_lines_to_rich_html_renders_fenced_blocks_as_pre():
    from plugins.platforms.telegram.command_cards import lines_to_rich_html

    html_out = lines_to_rich_html("Diff", ["```diff", "- old", "+ new", "```", "after"])
    assert "<pre>- old\n+ new</pre>" in html_out
    assert "```" not in html_out
    assert "after" in html_out


def test_lines_to_rich_html_strips_cross_mark_emoji():
    from plugins.platforms.telegram.command_cards import lines_to_rich_html

    html_out = lines_to_rich_html("Codex", ["❌ bad flag", "✅ ok"])
    assert "❌" not in html_out
    assert "✅" not in html_out
    assert "bad flag" in html_out


# --- /platform, /autoprune, /sethome, /voice ----------------------------------

@pytest.mark.asyncio
async def test_platform_list_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_platform_command(_event("/platform"))
    assert result is None
    assert "Platform" in _card_html(adapter)


@pytest.mark.asyncio
async def test_autoprune_status_sends_rich_card_on_telegram(monkeypatch):
    from gateway import slash_commands

    runner, adapter = _make_runner()
    monkeypatch.setattr(
        slash_commands, "_execute",
        lambda command, **kw: SimpleNamespace(text="Auto-prune: off"),
    )
    result = await runner._handle_autoprune_command(_event("/autoprune"))
    assert result is None
    assert "Auto-prune" in _card_html(adapter)


@pytest.mark.asyncio
async def test_sethome_success_sends_rich_card_on_telegram(monkeypatch):
    from gateway import slash_commands as slash_commands_mod

    runner, adapter = _make_runner()
    runner.config = SimpleNamespace(platforms={})
    monkeypatch.setattr(slash_commands_mod, "persist_home_channel", lambda home, **kw: None)
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda *a, **k: None)
    result = await runner._handle_set_home_command(_event("/sethome"))
    assert result is None
    assert "home" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_voice_status_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_voice_command(_event("/voice status"))
    assert result is None
    assert "voice" in _card_html(adapter).lower()


# --- /verbose, /footer ---------------------------------------------------------

@pytest.mark.asyncio
async def test_verbose_not_enabled_sends_rich_card_on_telegram(monkeypatch):
    import gateway.session_setting_commands as setting_cmds

    runner, adapter = _make_runner()
    monkeypatch.setattr(setting_cmds, "local_live", lambda runner, source: None)
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    result = await runner._handle_verbose_command(_event("/verbose"))
    assert result is None
    assert "verbose" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_footer_status_sends_rich_card_on_telegram(monkeypatch):
    runner, adapter = _make_runner()
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    result = await runner._handle_footer_command(_event("/footer status"))
    assert result is None
    assert "footer" in _card_html(adapter).lower()


# --- /reload-skills, /reload-mcp, /debug, /update, /sync -----------------------

@pytest.mark.asyncio
async def test_reload_skills_no_new_sends_rich_card_on_telegram(monkeypatch):
    from agent import skill_commands

    runner, adapter = _make_runner()
    monkeypatch.setattr(
        skill_commands, "reload_skills",
        lambda: {"added": [], "removed": [], "total": 3},
    )
    result = await runner._handle_reload_skills_command(_event("/reload-skills"))
    assert result is None
    assert "3" in _card_html(adapter)


@pytest.mark.asyncio
async def test_reload_mcp_no_confirm_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    runner._read_user_config = lambda: {"approvals": {"mcp_reload_confirm": False}}
    result = await runner._handle_reload_mcp_command(_event("/reload-mcp"))
    assert result is None
    assert "MCP reloaded" in _card_html(adapter)


@pytest.mark.asyncio
async def test_debug_local_refused_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_debug_command(_event("/debug local"))
    assert result is None
    assert "local" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_update_managed_sends_rich_card_on_telegram(monkeypatch):
    import hermes_cli.config as cli_config

    runner, adapter = _make_runner()
    monkeypatch.setattr(cli_config, "is_managed", lambda: True)
    result = await runner._handle_update_command(_event("/update"))
    assert result is None
    assert "update" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_sync_admin_only_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_sync_command(_event("/sync"))
    assert result is None
    assert "admin" in _card_html(adapter).lower()


# --- /login, /pause ------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_group_chat_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_login_command(_event("/login", chat_type="group"))
    assert result is None
    assert "direct message" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_pause_engage_sends_rich_card_on_telegram(monkeypatch):
    from agent import estop

    runner, adapter = _make_runner()
    monkeypatch.setattr(estop, "get_state", lambda: None)
    monkeypatch.setattr(estop, "engage", lambda reason=None: None)
    result = await runner._handle_pause_command(_event("/pause maintenance"))
    assert result is None
    assert "pause" in _card_html(adapter).lower()


# --- /codex-runtime, /personality ----------------------------------------------

@pytest.mark.asyncio
async def test_codex_runtime_errors_send_rich_card_without_emoji(monkeypatch):
    import hermes_cli.codex_runtime_switch as crs

    runner, adapter = _make_runner()
    monkeypatch.setattr(crs, "parse_args", lambda args: (None, ["bad flag --nope"]))
    result = await runner._handle_codex_runtime_command(_event("/codex-runtime --nope"))
    assert result is None
    html = _card_html(adapter)
    assert "bad flag" in html
    assert "❌" not in html


@pytest.mark.asyncio
async def test_personality_list_sends_rich_card_on_telegram(monkeypatch):
    runner, adapter = _make_runner()
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    result = await runner._handle_personality_command(_event("/personality"))
    assert result is None
    assert "personality" in _card_html(adapter).lower()


# --- /rollback, /diff, /restart -------------------------------------------------

@pytest.mark.asyncio
async def test_rollback_not_enabled_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_rollback_command(_event("/rollback"))
    assert result is None
    assert "checkpoint" in _card_html(adapter).lower() or "rollback" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_diff_session_not_enabled_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_diff_command(_event("/diff session"))
    assert result is None
    assert "diff" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_restart_draining_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    runner._restart_requested = True
    runner._running_agent_count = lambda: 2
    result = await runner._handle_restart_command(_event("/restart"))
    assert result is None
    assert "2" in _card_html(adapter)
