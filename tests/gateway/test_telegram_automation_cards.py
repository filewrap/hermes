"""Phase 5 Group C: Telegram rich cards for /goal, /subgoal, /heartbeat, /loop,
/refine, /review, /bg, /btw, /kanban, /memory, /bundles, /curator,
/suggestions, /blueprint, /queue, /steer, /egress, /moa.

Invariant tests, written red-first: on Telegram each handler sends one native
rich card through ``adapter.send_command_card`` and returns None (no plain-text
fallback); on other platforms the plain-text reply is unchanged.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester", chat_type="dm",
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform), message_id="m1")


class _Store:
    """Minimal async session store; ``_store`` must be the runner's session_store
    so ``GatewayRunner.async_session_store`` accepts this facade."""

    def __init__(self, session_id="sess-1", store=None):
        self._session_id = session_id
        self._store = store

    async def get_or_create_session(self, source):
        return SimpleNamespace(
            session_key="agent:main:telegram:dm:u1", session_id=self._session_id,
            created_at=datetime.now(), updated_at=datetime.now(),
            platform=Platform.TELEGRAM, chat_type="dm", total_tokens=0,
            last_prompt_tokens=0,
        )

    async def load_transcript(self, session_id):
        return []


def _make_runner(*, telegram_card: bool = True, platform: Platform = Platform.TELEGRAM,
                 session_id: str = "sess-1"):
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
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._agent_cache = {}
    runner._cached_agent_for = lambda key: None
    runner._rehydrate_session_model_override = lambda key: None
    runner._session_model_override = lambda key: {}
    runner._resolve_route_context = AsyncMock(return_value=None)
    runner._queue_depth = lambda *args, **kwargs: 0
    runner._session_db = None
    runner._resident_agent_for = lambda key: None
    runner.session_store = MagicMock()
    runner._async_session_store = _Store(session_id=session_id, store=runner.session_store)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_key="agent:main:telegram:dm:u1", session_id=session_id)
    runner.session_store.load_transcript.return_value = []
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn: fn())
    runner._track_background_task = lambda coro: runner._background_tasks.add(coro)
    runner._run_background_task = MagicMock(return_value=AsyncMock())
    runner._gateway_plain_command_handlers = dict
    runner._gateway_idle_command_handlers = dict
    runner._write_approval_setter = lambda kind, event: (lambda *a, **k: None)
    runner._reply_metadata = lambda event: None
    runner._thread_metadata_for_source = lambda source: None
    return runner, adapter


def _card_html(adapter) -> str:
    card = adapter.send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs["html"]


# --- Pure card builders -------------------------------------------------------
def test_lines_to_rich_html_strips_pictographic_emoji():
    from plugins.platforms.telegram.command_cards import lines_to_rich_html

    html_out = lines_to_rich_html("Goal", ["⏳ Goal (parked): build it", "⚠ watch out"])
    assert "⏳" not in html_out
    assert "⚠" not in html_out
    assert "Goal (parked): build it" in html_out


# --- /goal, /subgoal, /heartbeat, /loop ---------------------------------------

@pytest.mark.asyncio
async def test_goal_unavailable_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    runner._get_goal_manager_for_event = AsyncMock(return_value=(None, None))
    result = await runner._handle_goal_command(_event("/goal"))
    assert result is None
    assert "unavailable" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_goal_unavailable_stays_plain_off_telegram():
    runner, _adapter = _make_runner(telegram_card=False, platform=Platform.SIGNAL)
    runner._get_goal_manager_for_event = AsyncMock(return_value=(None, None))
    result = await runner._handle_goal_command(_event("/goal", platform=Platform.SIGNAL))
    assert isinstance(result, str) and "unavailable" in result.lower()


@pytest.mark.asyncio
async def test_subgoal_no_goal_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    runner._get_goal_manager_for_event = AsyncMock(return_value=(None, None))
    result = await runner._handle_subgoal_command(_event("/subgoal"))
    assert result is None
    assert "unavailable" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_heartbeat_status_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    mgr = MagicMock()
    mgr.status_line.return_value = "Heartbeat: every 10m — check the inbox"
    mgr.session_id = "sess-1"
    runner._get_heartbeat_manager_for_event = AsyncMock(return_value=(mgr, None))
    result = await runner._handle_heartbeat_command(_event("/heartbeat"))
    assert result is None
    html = _card_html(adapter)
    assert "Heartbeat" in html
    assert "10m" in html


@pytest.mark.asyncio
async def test_loop_no_session_sends_rich_card_on_telegram():
    runner, adapter = _make_runner(session_id="")
    runner._warm_goals_session_db = AsyncMock()
    result = await runner._handle_loop_command(_event("/loop"))
    assert result is None
    assert "session" in _card_html(adapter).lower()


# --- /refine, /review ---------------------------------------------------------

@pytest.mark.asyncio
async def test_refine_started_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    agent = MagicMock()
    agent._session_messages = ["user: hi"]
    agent.valid_tool_names = set()
    agent._spawn_background_review = MagicMock()
    runner._cached_agent_for = lambda key: agent
    result = await runner._handle_refine_command(_event("/refine"))
    assert result is None
    html = _card_html(adapter)
    assert "refine" in html.lower() or "review" in html.lower()


@pytest.mark.asyncio
async def test_review_running_agent_refusal_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    runner._running_agents = {"agent:main:telegram:dm:u1": object()}
    result = await runner._handle_review_command(_event("/review"))
    assert result is None
    html = _card_html(adapter)
    assert "running" in html.lower()


# --- /bg, /btw -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_background_usage_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_background_command(_event("/bg"))
    assert result is None
    assert "usage" in _card_html(adapter).lower() or "/bg" in _card_html(adapter)


@pytest.mark.asyncio
async def test_background_started_sends_rich_card_with_task_id_copy():
    runner, adapter = _make_runner()
    result = await runner._handle_background_command(_event("/bg summarize this"))
    assert result is None
    card = adapter.send_command_card
    assert card.await_args is not None
    copies = card.await_args.kwargs.get("copies") or ()
    assert copies and copies[0][1].startswith("bg_")


@pytest.mark.asyncio
async def test_btw_usage_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_btw_command(_event("/btw"))
    assert result is None
    assert "/btw" in _card_html(adapter)


# --- /kanban, /memory, /bundles, /curator --------------------------------------

@pytest.mark.asyncio
async def test_kanban_error_sends_rich_card_on_telegram(monkeypatch):
    from hermes_cli import kanban

    runner, adapter = _make_runner()
    monkeypatch.setattr(kanban, "run_slash", lambda text: (_ for _ in ()).throw(RuntimeError("db down")))
    result = await runner._handle_kanban_command(_event("/kanban"))
    assert result is None
    assert "db down" in _card_html(adapter)


@pytest.mark.asyncio
async def test_memory_unknown_subcommand_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_memory_command(_event("/memory nonsense"))
    assert result is None
    assert "memory" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_bundles_error_sends_rich_card_on_telegram(monkeypatch):
    from gateway import slash_commands

    runner, adapter = _make_runner()
    monkeypatch.setattr(
        slash_commands, "_execute",
        lambda command, **kw: SimpleNamespace(data={"error": "no bundles dir"}, text="Bundles unavailable"),
    )
    result = await runner._handle_bundles_command(_event("/bundles"))
    assert result is None
    assert "Bundles unavailable" in _card_html(adapter)


@pytest.mark.asyncio
async def test_curator_status_sends_rich_card_on_telegram(monkeypatch):
    from hermes_cli import curator

    runner, adapter = _make_runner()
    monkeypatch.setattr(curator, "cli_main", lambda tokens: print("Curator: 3 skills healthy"))
    result = await runner._handle_curator_command(_event("/curator"))
    assert result is None
    assert "Curator" in _card_html(adapter)


@pytest.mark.asyncio
async def test_suggestions_failure_sends_rich_card_on_telegram(monkeypatch):
    from hermes_cli import suggestions_cmd

    runner, adapter = _make_runner()
    monkeypatch.setattr(
        suggestions_cmd, "handle_suggestions_command",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("suggestions down")),
    )
    result = await runner._handle_suggestions_command(_event("/suggestions"))
    assert result is None
    assert "suggestions down" in _card_html(adapter)


@pytest.mark.asyncio
async def test_suggestions_passes_args_to_shared_handler(monkeypatch):
    from hermes_cli import suggestions_cmd

    runner, adapter = _make_runner()
    seen = {}

    def _fake(args, *, origin=None, surface="cli"):
        seen["args"] = args
        seen["surface"] = surface
        return "Suggestions: 2 open"

    monkeypatch.setattr(suggestions_cmd, "handle_suggestions_command", _fake)
    result = await runner._handle_suggestions_command(_event("/suggestions list"))
    assert result is None
    assert seen["args"] == "list"
    assert seen["surface"] == "gateway"
    assert "Suggestions: 2 open" in _card_html(adapter)
    result = await runner._handle_suggestions_command(_event("/suggestions"))
    assert result is None
    assert seen["args"] == ""
    assert "Suggestions: 2 open" in _card_html(adapter)


# --- /blueprint, /queue, /steer, /egress, /moa (canonical dispatcher) ---------

@pytest.mark.asyncio
async def test_blueprint_no_seed_sends_rich_card_on_telegram():
    from hermes_cli.blueprint_cmd import BlueprintCommandResult

    runner, adapter = _make_runner()
    runner._handle_blueprint_command = AsyncMock(
        return_value=BlueprintCommandResult(text="No blueprint matched."))
    handled, result = await runner._hm_dispatch_canonical_command(
        _event("/blueprint"), _source(), "agent:main:telegram:dm:u1", "blueprint")
    assert handled is True
    assert result is None
    assert "blueprint" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_queue_idle_usage_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    handled, result = await runner._hm_dispatch_canonical_command(
        _event("/queue"), _source(), "agent:main:telegram:dm:u1", "queue")
    assert handled is True
    assert result is None
    assert "/queue" in _card_html(adapter)


@pytest.mark.asyncio
async def test_steer_idle_usage_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    handled, result = await runner._hm_dispatch_canonical_command(
        _event("/steer"), _source(), "agent:main:telegram:dm:u1", "steer")
    assert handled is True
    assert result is None
    assert "/steer" in _card_html(adapter)


@pytest.mark.asyncio
async def test_egress_status_sends_rich_card_on_telegram(monkeypatch):
    from hermes_cli import proxy_cli

    runner, adapter = _make_runner()
    monkeypatch.setattr(proxy_cli, "format_status_text", lambda: "Egress: proxy healthy")
    handled, result = await runner._hm_dispatch_canonical_command(
        _event("/egress"), _source(), "agent:main:telegram:dm:u1", "egress")
    assert handled is True
    assert result is None
    assert "Egress" in _card_html(adapter)


# --- Busy-path /queue, /steer, /goal, /loop ------------------------------------

@pytest.mark.asyncio
async def test_busy_queue_usage_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._busy_queue_command(
        _event("/queue"), "agent:main:telegram:dm:u1", _source())
    assert result is None
    assert "/queue" in _card_html(adapter)


@pytest.mark.asyncio
async def test_busy_steer_usage_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._busy_steer_command(
        _event("/steer"), "agent:main:telegram:dm:u1", _source())
    assert result is None
    assert "/steer" in _card_html(adapter)


@pytest.mark.asyncio
async def test_busy_goal_reject_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._busy_goal_command(
        _event("/goal set a new goal"), "agent:main:telegram:dm:u1", _source())
    assert result is None
    assert "goal" in _card_html(adapter).lower()


@pytest.mark.asyncio
async def test_busy_loop_reject_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._busy_loop_command(
        _event("/loop every 5m check mail"), "agent:main:telegram:dm:u1", _source())
    assert result is None
    assert "loop" in _card_html(adapter).lower()
