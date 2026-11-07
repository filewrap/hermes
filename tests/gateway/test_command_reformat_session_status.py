"""Behaviour contracts for the /session + /status command reformat (Worker B).

Every reformed Telegram reply renders as a structured rich card: a heading, labelled
rows as ``- `` list items, blank lines between sections, no double blank lines, and
no pictographic emoji in the reformed lines. Short one-line replies (discard, retry,
save-complete) and the already-rich /status HTML table are covered by their existing
tests and asserted here only where the reformat touches them.
"""

import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands_session import (
    _branch_reply_lines,
    _compress_preview_reply,
    _manual_compression_reply_lines,
    _title_reply_lines,
    _topic_help_rich_lines,
    _topic_reply_lines,
    _undo_removed_lines,
)
from gateway.slash_commands_status import (
    _context_compressor_lines,
    _insights_rich_lines,
    _last_activity_rich_lines,
    _telegram_status_tables,
    _usage_agent_stats_lines,
)
from plugins.platforms.telegram.command_cards import _EMOJI_RE, lines_to_rich_html

# The renderer's emoji ranges, plus the U+2139 info mark the reformat also strips.
EMOJI_CHECK_RE = re.compile(_EMOJI_RE.pattern + r"|\u2139")


def _no_emoji(text: str) -> bool:
    return EMOJI_CHECK_RE.search(text) is None


def _no_double_blank(text: str) -> bool:
    return "\n\n\n" not in text and not text.startswith("\n") and not text.endswith("\n")


def _source(platform: Platform = Platform.TELEGRAM, **kwargs) -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester", chat_type="dm",
        **kwargs,
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM, **kwargs) -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform, **kwargs), message_id="m1")


class _Store:
    """Minimal async session store; ``_store`` must be the runner's session_store
    so ``GatewayRunner.async_session_store`` accepts this facade."""

    def __init__(self, history=None, store=None, rewind=None):
        self._history = history if history is not None else []
        self._store = store
        self._rewind = rewind

    async def get_or_create_session(self, source):
        return SimpleNamespace(
            session_key="agent:main:telegram:dm:u1", session_id="sess-1",
            created_at=datetime.now(), updated_at=datetime.now(),
            platform=Platform.TELEGRAM, chat_type="dm", total_tokens=0,
            last_prompt_tokens=0, origin=None, display_name=None,
        )

    async def load_transcript(self, session_id):
        return list(self._history)

    async def rewind_session(self, session_id, n):
        return dict(self._rewind) if self._rewind is not None else None

    async def switch_session(self, session_key, target_id):
        return SimpleNamespace(session_key=session_key, session_id=target_id)


def _make_runner(*, platform: Platform = Platform.TELEGRAM):
    """Bare GatewayRunner with a fake adapter exposing send_command_card."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    card = AsyncMock(return_value=True)
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
    runner._async_session_store = _Store(store=runner.session_store)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_key="agent:main:telegram:dm:u1", session_id="sess-1")
    runner.session_store.load_transcript.return_value = []
    runner._run_in_executor_with_context = AsyncMock(return_value="Insights over 30 days.")
    return runner, adapter


def _card_html(adapter) -> str:
    adapter.send_command_card.assert_awaited_once()
    return adapter.send_command_card.await_args.kwargs["html"]


# --- Group 1: session confirmations (undo, branch, title, topic, reset, compress) ---

def test_session_confirmation_rows_are_list_items_with_blank_line_separators():
    """Reformed confirmations keep every fact as a labelled ``- `` row, sections apart."""
    branch = "\n".join(_branch_reply_lines(t(
        "gateway.branch.branched_many", title="side quest", count=3,
        parent="parent-1", new="new-1")))
    undo = "\n".join(_undo_removed_lines(t(
        "gateway.undo.removed", turns=1, count=2, preview="q3")))
    title = "\n".join(_title_reply_lines(t(
        "gateway.title.current_with_title", session_id="sess-1", title="My Title")))
    topic = "\n".join(_topic_reply_lines(t(
        "gateway.topic.bound_status", label="My Topic", session_id="sess-9")))
    for text in (branch, undo, title, topic):
        assert _no_double_blank(text), text
        assert _no_emoji(text), text
        html = lines_to_rich_html("Card", text.split("\n"))
        assert "<ul>" in html and "<li>" in html, html
    assert branch.split("\n") == [
        "⑂ Branched to **side quest** (3 messages copied)", "",
        "- Original: `parent-1`", "- Branch: `new-1`", "",
        "Use `/resume` to switch back to the original.",
    ]
    assert undo.split("\n") == [
        "- Undid 1 turn(s) (2 message(s)).", '- Backed up to: "q3"', "",
        "Copy/edit the text above and send it to re-prompt from here.",
    ]
    assert title.split("\n") == ["- Session: `sess-1`", "- Title: **My Title**"]
    assert topic.split("\n") == [
        "This topic is linked to:", "- Session: My Topic", "- ID: `sess-9`", "",
        "Use /new to replace this topic with a fresh session.",
        "For parallel work, open All Messages and send a message there to create another topic.",
    ]


def test_topic_help_renders_usage_fence_and_step_list():
    """The /topic help wall becomes sections: a usage fence and a step list."""
    text = _topic_help_rich_lines(t("gateway.topic.help"))
    assert _no_double_blank(text)
    assert "## Usage" in text
    assert "```" in text
    assert "## How it works" in text
    assert "- Run /topic once in this DM" in text
    assert "1. Run /topic" not in text
    assert "Settings are enabled and flips on multi-session mode." in text
    html = lines_to_rich_html("Topic", text.split("\n"))
    assert "<h2>Usage</h2>" in html and "<pre>" in html and "<ul>" in html
    # Unknown shapes pass through untouched rather than being mangled.
    assert _topic_help_rich_lines("plain text, no markers") == "plain text, no markers"


def test_compress_replies_headline_first_then_items_then_notes():
    """Compress confirmations: headline, then figures as items, notes as own paragraphs."""
    summary = {"headline": "Compressed: 10 → 4 messages",
               "token_line": "Approx request size: ~8,000 → ~3,000 tokens", "note": None}
    lines = _manual_compression_reply_lines(summary, SimpleNamespace(), "auth")
    assert lines[0] == "Compressed: 10 → 4 messages"
    assert "- Focus: \"auth\"" in lines
    assert "- Approx request size: ~8,000 → ~3,000 tokens" in lines
    assert "None" not in "\n".join(lines) and _no_emoji("\n".join(lines))
    # Absent fields are omitted, never a placeholder.
    assert not any("Focus" in line for line in
                   _manual_compression_reply_lines(summary, SimpleNamespace(), None))
    aborted = _manual_compression_reply_lines(
        summary, SimpleNamespace(_last_compress_aborted=True, _last_summary_error="boom"), None)
    assert any("Compression aborted (boom)" in line for line in aborted)
    assert _no_emoji("\n".join(aborted))
    aux = _manual_compression_reply_lines(
        summary, SimpleNamespace(_last_aux_model_failure_model="gemini-3-flash-preview",
                                 _last_aux_model_failure_error="404"), None)
    assert any("gemini-3-flash-preview" in line and "404" in line for line in aux)
    assert _no_emoji("\n".join(aux))

    history = [{"role": "user", "content": "hi"}] * 3 + [{"role": "assistant", "content": "yo"}] * 3
    preview = _compress_preview_reply(history, False, 3, None, "")
    assert preview.split("\n")[0] == "Preview — no changes made."
    assert "- Would compress 6 of 6 message(s)" in preview
    assert preview.rstrip().endswith("Run the command again without --preview to apply.")
    assert _no_emoji(preview) and _no_double_blank(preview)


@pytest.mark.asyncio
async def test_undo_and_branch_handlers_send_structured_cards():
    """End to end: /undo and /branch replies arrive as list-structured cards."""
    runner, _ = _make_runner()
    runner._async_session_store = _Store(
        store=runner.session_store,
        rewind={"turns_undone": 1, "rewound_count": 2, "target_text": "q3"})
    runner._record_model_friction = lambda *a, **k: None
    runner._evict_cached_agent = lambda *a, **k: None
    result = await runner._handle_undo_command(_event("/undo", platform=Platform.SLACK))
    assert result is not None
    assert "- Undid 1 turn(s) (2 message(s))." in result
    assert '- Backed up to: "q3"' in result
    assert "\n\nCopy/edit" in result

    runner, _ = _make_runner()
    runner.config = {}
    runner._branch_open_thread = AsyncMock(return_value=None)
    runner._clear_session_boundary_security_state = lambda *a, **k: None
    runner._evict_cached_agent = lambda *a, **k: None
    db = MagicMock()
    db.get_session_title = AsyncMock(return_value="side quest")
    db.get_next_title_in_lineage = AsyncMock(return_value="side quest (2)")
    db.get_session = AsyncMock(return_value={"system_prompt": None})
    db.create_session = AsyncMock()
    db.append_messages_batch = AsyncMock()
    db.set_session_title = AsyncMock()
    runner._session_db = db
    runner._async_session_store = _Store(
        store=runner.session_store,
        history=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}])
    result = await runner._handle_branch_command(_event("/branch", platform=Platform.SLACK))
    assert result is not None
    assert "- Original: `sess-1`" in result
    assert "- Branch: `" in result and "side quest" in result
    assert "\n\nUse `/resume`" in result


@pytest.mark.asyncio
async def test_topic_help_and_bound_status_handlers_send_structured_cards():
    """End to end: /topic help and the bound-topic status arrive structured."""
    runner, adapter = _make_runner()
    runner._is_user_authorized_for_source = lambda source: True
    runner._telegram_topic_help_text = lambda: t("gateway.topic.help")
    runner._session_db = MagicMock()
    result = await runner._handle_topic_command(_event("/topic help"))
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Usage</h2>" in html and "<pre>" in html and "<ul>" in html

    runner, adapter = _make_runner()
    runner._is_user_authorized_for_source = lambda source: True
    runner._telegram_topic_profile_name = lambda source: "default"
    runner._get_telegram_topic_capabilities = AsyncMock(return_value={"checked": False})
    db = MagicMock()
    db.enable_telegram_topic_mode = AsyncMock()
    db.get_telegram_topic_binding = AsyncMock(return_value={"session_id": "sess-9"})
    db.get_session_title = AsyncMock(return_value="My Topic")
    runner._session_db = db
    result = await runner._handle_topic_command(
        _event("/topic", platform=Platform.TELEGRAM, thread_id="777"))
    assert result is None
    html = _card_html(adapter)
    assert "<li>Session: My Topic</li>" in html
    assert "<li>ID: <code>sess-9</code></li>" in html


# --- Group 2: session listings (resume, sessions) ---

def test_resume_listing_keeps_numbered_most_recent_first_with_blank_line_footer():
    """The /resume list stays numbered (most recent first), with a blank line before the footer."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._resume_caller_is_admin = lambda source: False
    rows = [
        {"id": "s1", "title": "Research", "preview": "plan draft"},
        {"id": "s2", "title": "Coding"},
    ]
    text = runner._resume_listing_reply(_source(), rows, allow_all=False)
    assert text.split("\n").index("1. **Research** — _plan draft_") < text.split("\n").index("2. **Coding**")
    assert "\n\nUsage: `/resume <session name>`" in text
    assert "None" not in text and " — _" not in text.replace(" — _plan draft_", "")
    # A non-admin --all gets the scope note as its own paragraph, not glued to the list.
    noted = runner._resume_listing_reply(_source(), rows, allow_all=True)
    assert "\n\n_Note: `--all`" in noted

    from hermes_cli.session_listing import format_gateway_session_listing

    listing = format_gateway_session_listing(
        [{"id": "s1", "title": "Research", "preview": "", "is_current_session": True},
         {"id": "s2", "title": "Coding", "preview": "x"}])
    lines = listing.split("\n")
    assert lines[1] == "" and "**Research** (current)" in lines[2]
    assert "Resume: `/resume <session id>`" in listing
    assert "None" not in listing


# --- Group 3: status introspection (status, context, usage, agents, last, topup, insights) ---

def test_status_reply_stays_a_rich_table():
    """/status was already a rich card: real HTML tables on Telegram, labelled rows elsewhere."""
    lines = ["**Hermes Gateway Status**", "", "**Session ID:** `sess-1`",
             "**Model:** `gpt-test` (openai)", "**Context:** 1,000 / 100,000 (1%)",
             "**Lifetime tokens billed:** 1,900", "**Agent Running:** No", "",
             "**Connected Platforms:** telegram"]
    html = _telegram_status_tables(lines, ["telegram"])
    assert "<table>" in html and "<td>Session ID</td>" in html
    assert "<td><code>sess-1</code></td>" in html
    assert "<td>Connected</td>" in html


def test_last_and_agents_cards_have_list_structure_with_current_first():
    """Reformed /last and /agents: tool rows as list items, this chat's agent first."""
    block = ("Last activity: `terminal`\n1762345000s ago · 0.8s · Completed\n"
             "Arguments: command: ls\nResult: a.txt")
    lines = _last_activity_rich_lines(block)
    assert lines == ["- Last activity: `terminal` · 1762345000s ago · 0.8s · Completed",
                     "- Arguments: command: ls", "- Result: a.txt"]
    assert _no_emoji("\n".join(lines))


@pytest.mark.asyncio
async def test_last_handler_starts_with_running_tool_then_list():
    runner, adapter = _make_runner()
    summary = {
        "current_tool": "read_file",
        "last_tool_activity": {
            "tool": "terminal", "args": '{"command": "ls"}', "result": "a.txt",
            "error": False, "blocked": False, "duration_s": 0.8, "ts": 1000.0,
        },
        "last_activity_desc": "tool completed: terminal (0.8s)",
    }
    runner._running_agents["agent:main:telegram:dm:u1"] = SimpleNamespace(
        get_activity_summary=lambda: summary)
    result = await runner._handle_last_command(_event("/last"))
    assert result is None
    html = _card_html(adapter)
    assert "Currently running:" in html
    assert "<ul>" in html and "<li>" in html and "terminal" in html


@pytest.mark.asyncio
async def test_agents_handler_lists_current_chat_first():
    runner, adapter = _make_runner()
    import time as _time

    now = _time.time()
    runner._running_agents = {
        "agent:main:telegram:dm:u1": SimpleNamespace(session_id="sess-1", model="m1"),
        "agent:main:telegram:dm:other": SimpleNamespace(session_id="sess-2", model="m2"),
    }
    runner._running_agents_ts = {
        "agent:main:telegram:dm:u1": now - 8,
        "agent:main:telegram:dm:other": now - 300,
    }
    result = await runner._handle_agents_command(_event("/agents"))
    assert result is None
    html = _card_html(adapter)
    assert "<strong>Active agents:</strong> 2" in html
    assert html.index("agent:main:telegram:dm:u1") < html.index("agent:main:telegram:dm:other")
    assert "this chat" in html
    assert "<ul>" in html


def test_usage_and_context_sections_omit_absent_fields():
    """Usage/context: absent figures are omitted, never a placeholder."""
    ctx = SimpleNamespace(last_prompt_tokens=0, context_length=100000, compression_count=0)
    agent = SimpleNamespace(
        model="claude-sonnet-4.6", session_input_tokens=35000, session_output_tokens=10000,
        session_total_tokens=50000, session_api_calls=12, context_compressor=ctx,
        get_rate_limit_state=lambda: SimpleNamespace(has_data=False))
    lines = _usage_agent_stats_lines(agent)
    assert lines[0] == "## Session Token Usage"
    assert "- Model: `claude-sonnet-4.6`" in lines
    assert "- Compressions: 1" not in lines and "- Context:" not in "\n".join(lines)
    assert "None" not in "\n".join(lines) and _no_emoji("\n".join(lines))

    ctx2 = SimpleNamespace(threshold_tokens=0, threshold_percent=0, compression_count=0,
                           last_prompt_tokens=0, context_length=0)
    agent2 = SimpleNamespace(session_api_calls=0, session_input_tokens=0,
                             session_output_tokens=0, session_reasoning_tokens=0,
                             session_total_tokens=0)
    lines = _context_compressor_lines(agent2, ctx2, 0)
    assert lines[0] == "## Automatic compression"
    assert not any("threshold" in line.lower() for line in lines)
    assert "None" not in "\n".join(lines) and _no_emoji("\n".join(lines))


@pytest.mark.asyncio
async def test_usage_and_context_plain_text_have_sections_and_items(monkeypatch):
    """End to end (plain text): /usage and /context carry headings and list items."""
    import gateway.slash_commands_status as status_mod

    monkeypatch.setattr(status_mod, "fetch_account_usage", lambda *a, **k: None)
    monkeypatch.setattr("agent.account_usage.nous_credits_lines", lambda *a, **k: [])
    ctx = SimpleNamespace(last_prompt_tokens=30000, context_length=100000, compression_count=1)
    agent = SimpleNamespace(
        model="claude-sonnet-4.6", provider="openrouter", base_url="", api_key="",
        session_input_tokens=35000, session_output_tokens=10000, session_total_tokens=50000,
        session_api_calls=12, context_compressor=ctx,
        get_rate_limit_state=lambda: SimpleNamespace(has_data=False), _credits_state=None)
    runner, _ = _make_runner()
    runner._resident_agent_for = lambda key: agent
    result = await runner._handle_usage_command(_event("/usage", platform=Platform.SLACK))
    assert result is not None
    assert "## Session Token Usage" in result
    assert "- Model: `claude-sonnet-4.6`" in result
    assert "- Compressions: 1" in result
    assert "\U0001F4CA" not in result and _no_double_blank(result)

    ctx2 = SimpleNamespace(last_prompt_tokens=66570, context_length=262144,
                           threshold_tokens=200000, threshold_percent=0.8,
                           compression_count=2, _last_compression_savings_pct=25.0)
    agent2 = SimpleNamespace(model="test-model", context_compressor=ctx2)
    runner, _ = _make_runner()
    runner._resident_agent_for = lambda key: agent2
    result = await runner._handle_context_command(_event("/context", platform=Platform.SLACK))
    assert result is not None
    assert result.split("\n")[0] == "# Context Window"
    assert "## Current model window" in result
    assert "- Model: `test-model`" in result
    assert "- In use: 66,570 / 262,144 (25%)" in result
    assert "## Automatic compression" in result
    assert "## Session throughput" in result
    assert _no_double_blank(result)


@pytest.mark.asyncio
async def test_topup_and_insights_cards_have_headings_and_no_bare_links(monkeypatch):
    """End to end: /topup balance rows are list items with the portal as a button;
    /insights sections become headings with list rows; section-less text passes through."""
    runner, adapter = _make_runner()

    def _fake_view(*args, **kwargs):
        return SimpleNamespace(
            logged_in=True,
            balance_lines=["Balance: $5.00", "\U0001F4C8 chart line"],
            identity_line="Signed in as tester",
            topup_url="https://example.test/topup",
        )

    monkeypatch.setattr("agent.account_usage.build_credits_view", _fake_view)
    result = await runner._handle_topup_command(_event("/topup"))
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Nous balance</h2>" in html
    assert "<li>Balance: $5.00</li>" in html
    assert "https://example.test/topup" not in html
    assert ("Open billing portal", "https://example.test/topup") in (
        adapter.send_command_card.await_args.kwargs["links"])
    assert "\U0001F4C8" not in html

    engine_text = (
        "\U0001F4CA **Hermes Insights** — Last 30 days\n\n"
        "**Sessions:** 5 | **Messages:** 100 | **Tool calls:** 20\n"
        "**Tokens:** 1,000 (in: 700 / out: 300)\n\n"
        "**\U0001F916 Models:**\n"
        "  model-a — 3 sessions, 600 tokens\n"
        "  model-b — 2 sessions, 400 tokens\n\n"
        "**\U0001F527 Top Tools:**\n"
        "  terminal — 15 calls (75.0%)\n"
    )
    runner, adapter = _make_runner()
    runner._run_in_executor_with_context = AsyncMock(return_value=(engine_text, None, ()))
    result = await runner._handle_insights_command(_event("/insights"))
    assert result is None
    html = _card_html(adapter)
    assert "<h2>Models</h2>" in html and "<h2>Top Tools</h2>" in html
    assert "<li>model-a" in html and "<li>terminal" in html
    assert "<li><strong>Sessions:</strong> 5</li>" in html
    assert "\U0001F4CA" not in html and "\U0001F916" not in html and "\U0001F527" not in html

    # No section headers: the text is returned unchanged (e.g. the empty report).
    assert _insights_rich_lines("Insights over 30 days.") == "Insights over 30 days."


def test_reformed_outputs_carry_no_emoji_and_no_double_blanks():
    """Across every reformed shape: no pictographic emoji, no double blank lines."""
    outputs = [
        "\n".join(_branch_reply_lines(t(
            "gateway.branch.branched_thread_many", title="t", count=2,
            parent="p", new="n", thread="<#777>"))),
        "\n".join(_undo_removed_lines(t(
            "gateway.undo.removed", turns=2, count=4, preview="q2"))),
        "\n".join(_title_reply_lines(t(
            "gateway.title.current_no_title", session_id="sess-1"))),
        "\n".join(_topic_reply_lines(t("gateway.topic.topics_disabled"))),
        _topic_help_rich_lines(t("gateway.topic.help")),
        "\n".join(_manual_compression_reply_lines(
            {"headline": "Compressed: 10 → 4 messages",
             "token_line": "Approx request size: ~8,000 → ~3,000 tokens",
             "note": "The generated summary was larger than what it would replace."},
            SimpleNamespace(), None)),
        _insights_rich_lines(
            "**Sessions:** 5 | **Messages:** 100\n\n**\U0001F916 Models:**\n  m — 1 session, 5 tokens\n"),
        "\n".join(_last_activity_rich_lines(
            "Last activity: `terminal`\n3s ago · 0.8s · Completed\nResult: ok")),
    ]
    for text in outputs:
        assert _no_emoji(text), text
        assert _no_double_blank(text), text
