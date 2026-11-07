"""Behaviour-contract tests for the /command rich reformat (slash_commands.py).

Every reformed Telegram response must render as a clean, well-structured rich
message: a bold headline, labelled rows as ``- `` list items, one blank line
between sections, no double blank lines, absent fields omitted (never a
placeholder). These tests pin the shape per command group — the card renderer
(``lines_to_rich_html``) turns ``- `` lines into list items and blank lines
into paragraph breaks, so the assertions below are on the text the handlers
return (the same text the card is built from).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import (
    _autoprune_card_lines,
    _bullets,
    _clean_lines,
    _cmd_desc_bullets,
    _curator_card_text,
    _headline_body,
    _kanban_card_text,
    _normalize_paragraphs,
    _pending_card_lines,
    _rollback_listing_lines,
)


def _make_source(platform=Platform.TELEGRAM):
    return SessionSource(
        platform=platform, user_id="12345", chat_id="67890", user_name="testuser",
        chat_type="dm",
    )


def _make_event(text, platform=Platform.TELEGRAM):
    return MessageEvent(text=text, source=_make_source(platform))


def _make_runner():
    """Bare GatewayRunner: card delivery has no live transport, so handlers fall
    back to returning their (structured) text."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="<redacted>")})
    runner.adapters = {}
    runner._voice_mode = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._session_db = None
    runner.session_store = None
    return runner


# ------------------------------------------------------------------ structure helpers

def test_clean_lines_and_bullets_drop_blanks_and_indent():
    assert _clean_lines("  a  \n\nb\n   \n") == ["a", "b"]
    assert _bullets("Mode: off\n\n  Detail: x") == ["- Mode: off", "- Detail: x"]
    assert _bullets("- already\n- bulleted") == ["- already", "- bulleted"]


def test_normalize_paragraphs_collapses_blank_runs_and_strips():
    assert _normalize_paragraphs("a\n\n\n  b  \n\n\nc\n\n") == "a\n\nb\n\nc"
    assert _normalize_paragraphs("single") == "single"


def test_headline_body_splits_on_first_sentence():
    head, rest = _headline_body("Commands approved (2 commands). The agent is resuming...")
    assert head == "Commands approved (2 commands)."
    assert rest == "The agent is resuming..."
    head, rest = _headline_body("No pending command to approve.")
    assert head == "No pending command to approve."
    assert rest == ""


def test_cmd_desc_bullets_code_span_the_command():
    assert _cmd_desc_bullets(["/platform list — show platform status",
                              "plain line"]) == [
        "- `/platform list` — show platform status", "- plain line"]


# ------------------------------------------------------------------ pending write-approval reflow

_PENDING = (
    "Pending memory writes (2):\n"
    "  keepme01  add a fact\n"
    "      replaces entry: old fact\n"
    "  dropme02 [auto]  remove a stale fact\n"
    "\n"
    "Apply: /memory approve <id>   Reject: /memory reject <id>\n"
)


def test_pending_list_reflows_to_headline_and_bullets():
    out = _pending_card_lines("memory", _PENDING)
    assert "**Pending memory writes (2)**" in out
    assert "- `keepme01` add a fact" in out
    assert "- `dropme02` [auto] remove a stale fact" in out
    assert "- **Apply:** `/memory approve <id>`" in out
    assert "- **Reject:** `/memory reject <id>`" in out
    assert "\n\n\n" not in out  # no double blank lines
    assert "  keepme01" not in out  # no flat indented rows left


def test_pending_state_and_set_rows_are_labelled():
    out = _pending_card_lines("memory", "memory.write_approval = on\n\nNo pending memory writes.")
    assert "- **memory.write_approval:** `on`" in out
    assert "No pending memory writes." in out
    out = _pending_card_lines("skills", "skills.write_approval set to 'off'.")
    assert "- **skills.write_approval:** `off`" in out


def test_pending_single_line_confirmations_pass_through():
    for line in ("Approved 1 memory write(s).", "No pending memory write with id 'nope'.",
                 "Rejected pending memory write 'x'."):
        assert _pending_card_lines("memory", line) == line


# ------------------------------------------------------------------ kanban / curator reflow

def test_kanban_task_rows_become_bullets_with_code_id():
    output = ("● t_abcd1234  ready     alice                 Fix the thing\n"
              "◐ t_0123abcd  blocked   (unassigned)          Waiting on review\n"
              "(no matching tasks)\n")
    out = _kanban_card_text(output)
    assert "- ● `t_abcd1234`  ready" in out
    assert "- ◐ `t_0123abcd`  blocked" in out
    assert "(no matching tasks)" in out


def test_kanban_json_and_help_pass_through_shape():
    assert _kanban_card_text('{"tasks": []}') == '{"tasks": []}'
    help_out = _kanban_card_text("**/kanban** — manage the shared task board.\n\n"
                                 "  `list` (alias `ls`)   List tasks\n")
    assert "**/kanban** — manage the shared task board." in help_out
    assert "- `list` (alias `ls`)   List tasks" in help_out


def test_curator_report_reflows_indented_rows_and_titles():
    report = ("curator: enabled\n"
              "  runs:           3\n"
              "  last run:       2026-11-04 10:00\n"
              "\n"
              "by state:\n"
              "  active      2\n")
    out = _curator_card_text(report)
    assert "curator: enabled" in out
    assert "- **runs:** 3" in out
    assert "- **last run:** 2026-11-04 10:00" in out
    assert "## by state" in out
    assert "\n\n\n" not in out


# ------------------------------------------------------------------ autoprune / rollback reflow

def test_autoprune_status_reflows_to_labelled_rows():
    reply = ("Auto-prune is on (default), retention 7 days. state.db 1.2 GB; "
             "3.4 GB free of 10 GB (low). Usage: /autoprune on | off | later")
    out = _autoprune_card_lines(reply)
    assert out.split("\n")[0] == "**Auto-prune is on (default)**"
    assert "- **Retention:** 7 days" in out
    assert "- **Disk:** state.db 1.2 GB; 3.4 GB free of 10 GB (low)." in out
    assert "- **Usage:** `/autoprune on | off | later`" in out


def test_autoprune_unknown_shape_passes_through():
    reply = "OK, I'll ask again in a week if the disk is still low."
    assert _autoprune_card_lines(reply) == reply


def test_rollback_listing_reflows_to_bullets():
    listing = ("📸 Checkpoints for /work:\n"
               "\n"
               "  1. abc123  2026-11-04 10:00  before edit (3 files, +10/-2)\n"
               "  2. def456  2026-11-04 09:00  start\n"
               "\n"
               "  /rollback <N>             restore to checkpoint N\n")
    out = _rollback_listing_lines(listing)
    assert "**📸 Checkpoints for /work:**" in out
    assert "- `abc123`  2026-11-04 10:00  before edit (3 files, +10/-2)" in out
    assert "- `def456`  2026-11-04 09:00  start" in out
    assert "- `/rollback <N>` — restore to checkpoint N" in out


# ------------------------------------------------------------------ handler level: identity / status

@pytest.mark.asyncio
async def test_sync_success_is_headline_plus_counter_bullets(tmp_path, monkeypatch):
    from agent import mongo_credentials
    from gateway import mongo_sync

    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: True)
    monkeypatch.setattr(
        mongo_sync, "sync_profile", lambda _home: {"counters": {"synced": 3, "skipped": 1}})

    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace(platforms={"telegram": {"allow_admin_from": ["12345"]}})
    runner._resolve_profile_home_for_source = lambda _source: str(tmp_path)
    event = SimpleNamespace(
        source=SimpleNamespace(user_id="12345", platform="telegram", chat_type="private",
                               profile="default"))
    reply = await runner._handle_sync_command(event)
    assert reply.split("\n")[0] == "**Sync complete for profile default**"
    assert "- synced: 3" in reply
    assert "- skipped: 1" in reply


@pytest.mark.asyncio
async def test_platform_list_sections_failed_platforms():
    runner = _make_runner()
    runner.adapters = {Platform.DISCORD: MagicMock()}
    runner._failed_platforms = {Platform.WHATSAPP: {
        "config": PlatformConfig(enabled=True, token="<redacted>"),
        "attempts": 10, "next_retry": float("inf"), "paused": True,
        "pause_reason": "not paired"}}
    event = SimpleNamespace(content="/platform list")
    out = await runner._handle_platform_command(event)
    assert "- Connected: discord" in out
    assert "## Failed" in out
    assert "- whatsapp — PAUSED (not paired). Resume with `/platform resume whatsapp`." in out


@pytest.mark.asyncio
async def test_platform_usage_lists_subcommands_as_bullets():
    runner = _make_runner()
    runner.adapters = {}
    runner._failed_platforms = {}
    event = SimpleNamespace(content="/platform bogus")
    out = await runner._handle_platform_command(event)
    assert out.split("\n")[0] == "**Usage: /platform <list|pause|resume> [name]**"
    assert "- `/platform list` — show platform status" in out
    assert "- `/platform resume <name>` — re-queue a paused platform" in out


@pytest.mark.asyncio
async def test_whoami_headline_then_bulleted_rows(monkeypatch):
    runner = _make_runner()
    runner.config = SimpleNamespace(platforms={})
    out = await runner._handle_whoami_command(_make_event("/whoami"))
    lines = out.split("\n")
    assert lines[0] == "**You** — telegram (DM)"
    assert "- User ID: `12345`" in out
    assert "" in lines  # blank line separates identity from tier
    assert "- Tier: unrestricted (no admin list configured for this scope)" in out
    assert "- Slash commands: all available" in out


# ------------------------------------------------------------------ handler level: background / btw

@pytest.mark.asyncio
async def test_background_started_is_headline_plus_task_bullet():
    runner = _make_runner()

    async def _noop(*_a, **_k):
        return None

    runner._run_background_task = _noop
    runner._track_background_task = lambda coro: coro.close()
    runner._reply_anchor_for_event = lambda _event: None
    out = await runner._handle_background_command(
        _make_event("/bg Summarize the top HN stories today"))
    assert out.split("\n")[0] == '**🔄 Background task started: "Summarize the top HN stories today"**'
    assert "- **Task ID:** `bg_" in out
    assert "You can keep chatting" in out


@pytest.mark.asyncio
async def test_background_usage_is_headline_plus_example_bullet():
    runner = _make_runner()
    runner._reply_anchor_for_event = lambda _event: None
    out = await runner._handle_background_command(_make_event("/bg"))
    assert out.split("\n")[0] == "**Usage: /bg <prompt>**"
    assert "- **Example:** `/bg Summarize the top HN stories today`" in out


@pytest.mark.asyncio
async def test_btw_started_is_headline_plus_snapshot_line():
    runner = _make_runner()
    runner._track_background_task = lambda coro: coro.close()
    runner._reply_metadata = lambda _event: {}
    runner._delivery_adapter_for = lambda _source: None
    runner._resolve_session_agent_runtime = lambda *, source=None: ("m", {"api_key": "k"})

    store = AsyncMock()
    store.get_or_create_session.return_value = SimpleNamespace(session_id="s1")
    store.load_transcript.return_value = [{"role": "user", "content": "hello"}]
    store._store = runner.session_store
    runner._async_session_store = store
    out = await runner._handle_btw_command(_make_event("/btw which file was that error in?"))
    assert out.split("\n")[0] == '**💬 Side question: "which file was that error in?"**'
    assert "Answering from a snapshot of this conversation" in out


# ------------------------------------------------------------------ handler level: settings

@pytest.mark.asyncio
async def test_verbose_cycle_is_headline_detail_and_saved_suffix(tmp_path, monkeypatch):
    import hermes_yaml as yaml

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "display:\n  tool_progress_command: true\n  tool_progress: all\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    out = await runner._handle_verbose_command(_make_event("/verbose"))
    lines = out.split("\n")
    assert "VERBOSE" in lines[0]
    assert lines[1] == "Every tool call with full arguments."
    assert lines[2] == ""
    assert "saved for **telegram**" in lines[3]
    assert "\n\n\n" not in out
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["display"]["platforms"]["telegram"]["tool_progress"] == "verbose"


@pytest.mark.asyncio
async def test_voice_status_lists_mode_channel_and_members():
    runner = _make_runner()
    adapter = SimpleNamespace(get_voice_channel_info=lambda _gid: {
        "channel_name": "General", "member_count": 2,
        "members": [{"display_name": "Alice", "is_speaking": True},
                    {"display_name": "Bob", "is_speaking": False}]})
    runner._delivery_adapter_for = lambda _source: adapter
    runner._voice_key_for_source = lambda _source: "discord:123"
    runner._get_guild_id = lambda _event: 111
    out = await runner._handle_voice_command(_make_event("/voice status", Platform.DISCORD))
    assert "- **Voice mode:** Off (text only)" in out
    assert "- **Voice channel:** #General" in out
    assert "- **Participants:** 2" in out
    assert "- Alice (speaking)" in out
    assert "- Bob" in out


@pytest.mark.asyncio
async def test_voice_help_uses_real_list_bullets():
    runner = _make_runner()
    runner._delivery_adapter_for = lambda _source: None
    runner._voice_key_for_source = lambda _source: "telegram:123"
    runner._voice_mode = {}
    runner._save_voice_modes = lambda: None
    out = await runner._handle_voice_command(_make_event("/voice"))
    assert "- `/voice on` — voice reply when you send a voice message" in out
    assert "- `/voice status` — show the current mode" in out
    assert "• " not in out  # no ungrouped bullet glyphs left


@pytest.mark.asyncio
async def test_footer_status_lists_fields_and_platform(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("display:\n  tool_progress: all\n",
                                             encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    runner = _make_runner()
    out = await runner._handle_footer_command(_make_event("/footer status"))
    assert out.split("\n")[0].startswith("📎 Runtime footer: **")
    assert "- Fields: `" in out
    assert "- Platform: `telegram`" in out


# ------------------------------------------------------------------ handler level: approvals

def _clear_approval_state():
    from tools import approval as mod
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()


def _blocking_approval(session_key, command="rm -rf /"):
    from tools.approval import register_gateway_notify
    from tools.approval_gateway_wait import _ApprovalEntry
    register_gateway_notify(session_key, lambda _d: None)
    entry = _ApprovalEntry({"command": command})
    from tools.approval import _gateway_queues
    _gateway_queues.setdefault(session_key, []).append(entry)
    return entry


@pytest.mark.asyncio
async def test_approve_confirmation_is_headline_plus_resume_line():
    _clear_approval_state()
    runner = _make_runner()
    runner._session_key_for_source = lambda _source: "test-session"
    runner._delivery_adapter_for = lambda _source: None
    _blocking_approval("test-session")
    _blocking_approval("test-session")
    out = await runner._handle_approve_command(_make_event("/approve all"))
    assert out.split("\n")[0] == "**✅ Commands approved (2 commands).**"
    assert out.split("\n")[2] == "The agent is resuming..."
    _clear_approval_state()


@pytest.mark.asyncio
async def test_deny_confirmation_keeps_reason_on_its_own_line():
    _clear_approval_state()
    runner = _make_runner()
    runner._session_key_for_source = lambda _source: "test-session"
    runner._delivery_adapter_for = lambda _source: None
    _blocking_approval("test-session")
    out = await runner._handle_deny_command(_make_event("/deny that path is still in use"))
    assert out.split("\n")[0] == "**❌ Command denied.**"
    assert out.split("\n")[2] == 'Reason relayed to the agent: "that path is still in use"'
    _clear_approval_state()


# ------------------------------------------------------------------ handler level: listings

@pytest.mark.asyncio
async def test_kanban_help_lists_subcommands_as_bullets():
    runner = _make_runner()
    out = await runner._handle_kanban_command(_make_event("/kanban help"))
    assert out.split("\n")[0] == "**/kanban** — manage the shared task board."
    assert "- `list` (alias `ls`)   List tasks on the current board" in out
    assert "- `show <id>`           Task details + comments + events" in out


@pytest.mark.asyncio
async def test_bundles_list_bullets_bundles_and_keeps_skill_rows(monkeypatch):
    import gateway.slash_commands as sc

    def _fake_execute(command, **_kwargs):
        assert command == "bundles"
        return SimpleNamespace(data={"bundles": [
            {"slug": "dev", "description": "Dev tools", "skills": ["lint", "test"]}]})

    monkeypatch.setattr(sc, "_execute", _fake_execute)
    runner = _make_runner()
    out = await runner._handle_bundles_command(_make_event("/bundles"))
    assert "- `/dev` — Dev tools _(2 skills)_" in out
    assert "    · lint" in out
    assert "Invoke a bundle with `/<slug>` to load all its skills." in out


@pytest.mark.asyncio
async def test_bundles_none_state_is_headline_plus_action_rows(monkeypatch):
    import gateway.slash_commands as sc

    def _fake_execute(command, **_kwargs):
        return SimpleNamespace(data={"bundles": [], "dir": "/home/u/.hermes/bundles"})

    monkeypatch.setattr(sc, "_execute", _fake_execute)
    runner = _make_runner()
    out = await runner._handle_bundles_command(_make_event("/bundles"))
    assert out.split("\n")[0] == "**No skill bundles installed.**"
    assert "- Create one on the host with:" in out
    assert "- Directory: `/home/u/.hermes/bundles`" in out


@pytest.mark.asyncio
async def test_reload_mcp_no_confirm_bullets_server_changes(monkeypatch):
    runner = _make_runner()
    runner._read_user_config = lambda: {"approvals": {"mcp_reload_confirm": False}}

    async def _fake_reload(_event):
        return ("🔄 **MCP Servers Reloaded**\n\n"
                "♻️ Reconnected: fs, git\n"
                "➕ Added: brave\n"
                "\n🔧 5 tool(s) available from 3 server(s)")

    runner._execute_mcp_reload = _fake_reload
    out = await runner._handle_reload_mcp_command(_make_event("/reload-mcp"))
    assert out.split("\n")[0] == "🔄 **MCP Servers Reloaded**"
    assert "- **Reconnected:** fs, git" in out
    assert "- **Added:** brave" in out
    assert "🔧 5 tool(s) available from 3 server(s)" in out


@pytest.mark.asyncio
async def test_rollback_listing_bullets_checkpoints():
    runner = _make_runner()
    runner._terminal_cwd = lambda: "/work"

    class _Mgr:
        def unsupported_backend_reason(self):
            return ""

        def list_checkpoints(self, _cwd):
            return [{"timestamp": "2026-11-04T10:00:00", "short_hash": "abc123",
                     "reason": "before edit", "files_changed": 3,
                     "insertions": 10, "deletions": 2}]

    runner._checkpoint_manager = lambda _source: _Mgr()
    out = await runner._handle_rollback_command(_make_event("/rollback"))
    assert "**📸 Checkpoints for /work:**" in out
    assert "- `abc123`  2026-11-04 10:00  before edit" in out
    assert "(3 files, +10/-2)" in out
    assert "- `/rollback <N>` — restore to checkpoint N" in out


@pytest.mark.asyncio
async def test_yolo_and_auto_replies_stay_single_headline_lines():
    runner = _make_runner()
    runner.session_store = None
    runner._session_key_for_source = lambda _source: "test-session"
    runner._normalize_source_for_session_key = lambda source: source
    out = await runner._handle_yolo_command(_make_event("/yolo"))
    assert "YOLO mode" in str(out)
    assert "⚡" not in str(out) and "⚠️" not in str(out)
    assert str(out).count("\n\n") == 1  # headline, blank, detail
    out = await runner._handle_auto_command(_make_event("/auto on"))
    assert str(out) == "**Automatic approvals enabled for this session/thread.**"


# ------------------------------------------------------------------ handler level: /busy

def _busy_status_text(mode: str) -> str:
    """The status reply the handler builds, via the same locale strings."""
    from agent.i18n import t

    from gateway.slash_commands import _BUSY_MODE_BEHAVIOR, _normalize_paragraphs
    return _normalize_paragraphs(t(
        "gateway.busy.status", mode=mode,
        behavior=t(_BUSY_MODE_BEHAVIOR[mode][0])))


def _busy_set_text(mode: str) -> str:
    """The set-confirmation reply the handler builds, via the same locale strings."""
    from agent.i18n import t

    from gateway.slash_commands import _BUSY_MODE_BEHAVIOR, _normalize_paragraphs
    return _normalize_paragraphs(t(
        "gateway.busy.set", mode=mode,
        behavior=t(_BUSY_MODE_BEHAVIOR[mode][1])))


def _assert_busy_card_shape(text: str) -> list[str]:
    """Shared reformed-shape invariants for a /busy card; returns the list rows."""
    assert text, "reformed /busy card must not be empty"
    assert text.split("\n")[0].startswith("## "), "card opens with a heading"
    assert "\n\n\n" not in text, "no double blank lines"
    assert not text.endswith("\n"), "no trailing blank line"
    rows = [line for line in text.split("\n") if line.startswith("- ")]
    assert rows, "card lists the busy modes as rows"
    return rows


@pytest.mark.parametrize("mode", ["queue", "steer", "interrupt"])
def test_busy_status_card_lists_current_mode_first_and_marked(mode):
    """Status card: current mode first, marked, all three modes with behaviors."""
    from gateway.slash_commands import _rich_busy_text

    text = _rich_busy_text(_busy_status_text(mode))
    rows = _assert_busy_card_shape(text)
    assert rows[0].startswith(f"- `{mode} ✓`"), "current mode comes first and is marked"
    assert sum("✓" in row for row in rows) == 1, "exactly one mode is marked current"
    for name in ("queue", "steer", "interrupt"):
        assert any(row.startswith(f"- `{name}") for row in rows), f"{name} mode row missing"
    assert "queues for next turn" in text
    assert "steers into current run" in text
    assert "interrupts current run" in text
    assert text.split("\n")[-1].startswith("Change with "), "change hint is the last line"


def test_busy_set_card_confirms_new_mode_and_behavior():
    """Set card: confirmation head, new mode marked, long behavior as detail."""
    from gateway.slash_commands import _rich_busy_text

    text = _rich_busy_text(_busy_set_text("steer"))
    rows = _assert_busy_card_shape(text)
    assert text.split("\n")[0] == "## Busy input mode set"
    assert "- **Mode:** `steer` ✓" in rows
    assert "steered into the current run" in text


def test_busy_unknown_mode_card_keeps_usage_text():
    """Unknown-mode reply is headed, never a bare paragraph."""
    from gateway.slash_commands import _rich_busy_text

    text = _rich_busy_text(
        "Unknown mode `bogus`. Use `/busy queue`, `/busy steer`, or `/busy interrupt`.")
    assert text.split("\n")[0] == "## Busy input mode"
    assert "Unknown mode `bogus`" in text


@pytest.mark.asyncio
async def test_busy_status_without_card_keeps_original_text():
    """No Telegram card delivery: the handler returns the original status text."""
    runner = _make_runner()
    runner._busy_input_mode = "queue"
    out = await runner._handle_busy_command(_make_event("/busy"))
    assert str(out) == _busy_status_text("queue")


@pytest.mark.asyncio
async def test_busy_status_sends_reformed_card_with_change_copies():
    """With a Telegram card adapter: card sent, handler returns None, change commands are copy pills."""
    runner = _make_runner()
    runner._busy_input_mode = "steer"
    card = AsyncMock(return_value=True)
    adapter = SimpleNamespace(send_command_card=card)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._reply_anchor_for_event = lambda _event: None
    out = await runner._handle_busy_command(_make_event("/busy"))
    assert out is None, "card sent, so the handler sends nothing else"
    assert card.await_args is not None
    kwargs = card.await_args.kwargs
    html = kwargs["html"]
    assert "<h1>Busy</h1>" in html, "card carries the Busy title"
    assert "Busy input mode" in html
    assert "steer ✓" in html
    assert "queue" in html and "interrupt" in html
    assert tuple(kwargs["copies"]) == (
        ("/busy queue", "/busy queue"),
        ("/busy steer", "/busy steer"),
        ("/busy interrupt", "/busy interrupt"),
    )


@pytest.mark.asyncio
async def test_busy_set_sends_confirmation_card(monkeypatch):
    """Setting a mode cards the confirmation; the mode is saved and applied."""
    runner = _make_runner()
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    monkeypatch.setattr("cli.save_config_value", lambda k, v: True)
    monkeypatch.setattr(
        gateway_run, "_load_gateway_config",
        lambda: {"display": {"busy_input_mode": "queue"}})
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
    card = AsyncMock(return_value=True)
    adapter = SimpleNamespace(send_command_card=card)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._reply_anchor_for_event = lambda _event: None
    out = await runner._handle_busy_command(_make_event("/busy queue"))
    assert out is None, "card sent, so the handler sends nothing else"
    html = card.await_args.kwargs["html"]
    assert "Busy input mode set" in html
    assert "<code>queue</code> ✓" in html
    assert runner._busy_input_mode == "queue"
