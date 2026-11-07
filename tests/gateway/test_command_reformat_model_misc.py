"""Behaviour-contract tests for the rich reformatting of gateway model/misc
command responses (Worker D group): /personality, /codex-runtime, /reasoning,
/fast, /msettings (text fallback) and /egress (Part 2).

Invariants asserted per group, not per command:
- every reformed response opens with a heading (or, for card-titled replies,
  a labelled list row) and groups rows as a list;
- sections are separated by exactly one blank line (no double blank lines,
  no glued rows);
- absent fields are omitted, never printed as a bare placeholder;
- no emoji codepoints in the reformed text (the plain-text check mark is
  kept: it is not emoji presentation).
"""

import re
import threading
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.model_ui import render_settings, settings_payload
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

# Pictographic emoji stripped by the Telegram card renderer
# (plugins/platforms/telegram/command_cards.py::_EMOJI_RE).
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2B00-\u2BFF\u26A0\u274C\u2705\uFE0F]")


def _assert_reformed_shape(text: str) -> None:
    """Shared invariants for every reformed command response."""
    assert text, "reformed response must not be empty"
    first = text.split("\n")[0]
    assert first.startswith("# ") or first.startswith("- "), (
        "response must open with a heading, or — for card-titled replies — "
        "with a labelled list row"
    )
    assert "\n\n\n" not in text, "no double blank lines"
    assert not text.endswith("\n"), "no trailing blank line"
    assert _EMOJI_RE.search(text) is None, "no emoji in reformed text"
    assert not re.search(r"(?m)^  - ", text), "no indented '  - ' bullets (not a list)"
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("## "):
            assert index >= 2 and lines[index - 1] == "" and lines[index - 2] != "", (
                "section headings are preceded by exactly one blank line"
            )


def _make_event(text: str, platform=Platform.TELEGRAM) -> MessageEvent:
    source = SessionSource(
        platform=platform, user_id="12345", chat_id="67890", user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Bare GatewayRunner without calling __init__ (mirrors test_reasoning_command)."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._session_reasoning_overrides = {}
    runner._session_model_overrides = {}
    runner._session_service_tier_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


def _write_gateway_config(monkeypatch, tmp_path, body: str) -> None:
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)


def _write_cli_config(monkeypatch, tmp_path, body: str) -> None:
    """config.yaml under the HERMES_HOME the CLI loader (load_config) reads live."""
    home = tmp_path / "cli-home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))


class TestPersonalityList:
    @pytest.mark.asyncio
    async def test_list_is_a_headed_list_with_current_first(self, tmp_path, monkeypatch):
        _write_gateway_config(
            monkeypatch, tmp_path, "display:\n  personality: concise\n"
        )
        runner = _make_runner()

        text = await runner._handle_personality_command(_make_event("/personality"))

        assert text is not None
        _assert_reformed_shape(text)
        rows = [line for line in text.split("\n") if line.startswith("- ")]
        assert rows, "personalities must render as list rows"
        assert rows[0].startswith("- `concise"), "selected personality comes first"
        assert "✓" in rows[0], "selected personality is marked"
        assert "•" not in text, "old bullet glyphs are gone"
        assert "Available personalities" in text.split("\n")[0]
        usage = text.split("\n")[-1]
        assert usage.startswith("Usage: `/personality"), "usage is the last line"
        assert text.split("\n")[-2] == "", "usage is separated by a blank line"

    @pytest.mark.asyncio
    async def test_list_marks_none_current_when_no_personality_active(
        self, tmp_path, monkeypatch
    ):
        _write_gateway_config(monkeypatch, tmp_path, "{}\n")
        runner = _make_runner()

        text = await runner._handle_personality_command(_make_event("/personality"))

        rows = [line for line in text.split("\n") if line.startswith("- ")]
        assert rows[0].startswith("- `none"), "no-overlay entry is first when nothing is active"
        assert "✓" in rows[0]
        assert sum("✓" in row for row in rows) == 1, "exactly one entry is marked current"


class TestCodexRuntime:
    @pytest.mark.asyncio
    async def test_show_current_is_a_status_row_then_detail_lines(self, tmp_path, monkeypatch):
        _write_cli_config(monkeypatch, tmp_path, "{}\n")
        runner = _make_runner()

        text = await runner._handle_codex_runtime_command(_make_event("/codex-runtime"))

        assert text is not None
        _assert_reformed_shape(text)
        lines = text.split("\n")
        assert lines[0] == "- Status: `Current`"
        assert lines[1] == ""
        assert any(line.startswith("openai_runtime: ") for line in lines)
        assert not lines[0].startswith(("✓", "✗")), "no glued status glyph"

    @pytest.mark.asyncio
    async def test_change_reports_changed_and_unknown_arg_is_a_list_row(
        self, tmp_path, monkeypatch
    ):
        _write_cli_config(
            monkeypatch, tmp_path, "model:\n  openai_runtime: codex_app_server\n"
        )
        runner = _make_runner()

        changed = await runner._handle_codex_runtime_command(_make_event("/codex-runtime off"))
        assert changed.split("\n")[0] == "- Status: `Changed`"
        assert "codex_app_server → auto" in changed

        unknown = await runner._handle_codex_runtime_command(
            _make_event("/codex-runtime bogus")
        )
        assert unknown.startswith("- Unknown runtime 'bogus'.")
        assert _EMOJI_RE.search(unknown) is None

    @pytest.mark.asyncio
    async def test_failed_enable_reports_failed_without_running_migration(
        self, tmp_path, monkeypatch
    ):
        """Binary probe stubbed absent: the failure reply, not the migration."""
        import hermes_cli.codex_runtime_switch as crs

        _write_cli_config(monkeypatch, tmp_path, "{}\n")
        monkeypatch.setattr(crs, "check_codex_binary_ok", lambda codex_bin="codex": (False, "codex CLI not found"))
        runner = _make_runner()

        text = await runner._handle_codex_runtime_command(_make_event("/codex-runtime codex"))

        assert text.split("\n")[0] == "- Status: `Failed`"
        assert "Cannot enable codex_app_server runtime" in text
        assert "Install with: npm i -g @openai/codex" in text


class TestReasoningStatus:
    @pytest.mark.asyncio
    async def test_status_is_a_headed_list_of_labelled_rows(self, tmp_path, monkeypatch):
        _write_gateway_config(
            monkeypatch, tmp_path,
            "agent:\n  reasoning_effort: none\ndisplay:\n  show_reasoning: true\n",
        )
        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "xhigh"}
        runner._show_reasoning = False

        text = await runner._handle_reasoning_command(_make_event("/reasoning"))

        assert text is not None
        _assert_reformed_shape(text)
        assert text.split("\n")[0] == "# Reasoning settings"
        assert "- Effort: `none (disabled)`" in text
        assert "- Scope: global config" in text
        assert "- Display: `on ✓`" in text
        assert text.split("\n")[-1].startswith("_Usage:_ `/reasoning <")


class TestFastStatus:
    @pytest.mark.asyncio
    async def test_status_is_a_headed_list_with_current_mode(self, monkeypatch, tmp_path):
        runner = _make_runner()
        event = _make_event("/fast")
        session_key = runner._session_key_for_source(event.source)
        runner._session_model_overrides[session_key] = {
            "model": "gpt-6-astra", "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex", "api_key": "***",
        }
        monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_load_gateway_config", dict)
        monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "claude-sonnet-4-6")
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", dict)

        text = await runner._handle_fast_command(event)

        assert text is not None
        _assert_reformed_shape(text)
        assert text.split("\n")[0] == "# Priority Processing"
        assert "- Current mode: `normal`" in text
        assert text.split("\n")[-1].startswith("_Usage:_ `/fast <")


class TestModelSettingsText:
    def test_sampling_page_is_a_headed_list_with_italic_page_line(self):
        payload = settings_payload(settings={"temperature": 0.7}, page="sampling")

        text = render_settings(payload)

        _assert_reformed_shape(text)
        assert text.split("\n")[0] == "# Model settings"
        assert text.split("\n")[2] == "_Sampling — page 1 of 3_"
        assert "- Temperature: `0.7`" in text
        assert "- Top-p: `provider default`" in text
        assert "## Usage" in text
        assert "- Set a value: `/msettings set <setting> <value>`" in text
        assert "- Back to the model dashboard: `/model`" in text

    def test_capabilities_page_omits_empty_reasoning_options(self):
        payload = settings_payload(
            settings={}, page="capabilities",
            supported_reasoning_efforts=[], reasoning_supported=False,
        )

        text = render_settings(payload)

        _assert_reformed_shape(text)
        assert "- Reasoning: not supported by the current model/provider" in text
        assert "Supported values:" not in text, "empty option list is omitted, not printed bare"
        assert "None" not in text

    def test_capabilities_page_lists_supported_efforts_when_present(self):
        payload = settings_payload(
            settings={}, page="capabilities", reasoning_effort="high",
            supported_reasoning_efforts=["low", "medium", "high"],
            reasoning_supported=True,
        )

        text = render_settings(payload)

        assert "- Effort: `high`" in text
        assert "- Supported values: `low`, `medium`, `high`" in text


class TestEgressStatusText:
    def _stub_proxy(self, monkeypatch, *, mappings=(), uncovered=(), enabled=True,
                    configured=True, pid=1234, listening=True):
        from hermes_cli import proxy_cli

        status = SimpleNamespace(
            binary_path="/usr/bin/egress", binary_version="1.2.3",
            config_path="/home/user/.hermes/proxy.yaml",
            ca_cert_path="/home/user/.hermes/ca.pem", tunnel_port=8080,
            pid=pid, listening=listening, configured=configured,
        )
        monkeypatch.setattr(proxy_cli, "load_config", lambda: {"proxy": {"enabled": enabled}})
        monkeypatch.setattr(proxy_cli.ip, "get_status", lambda: status)
        monkeypatch.setattr(proxy_cli.ip, "load_mappings", lambda: list(mappings))
        monkeypatch.setattr(
            proxy_cli.ip, "discover_uncovered_providers", lambda: list(uncovered)
        )
        return proxy_cli

    def test_status_is_a_headed_list_with_sections(self, monkeypatch):
        from hermes_cli.proxy_cli import format_status_text

        self._stub_proxy(
            monkeypatch,
            mappings=[SimpleNamespace(
                proxy_token="tok_secret_value_1234", real_env_name="OPENAI_API_KEY",
                upstream_hosts=["api.openai.com"],
            )],
            uncovered=["anthropic"],
            configured=False,
        )

        text = format_status_text()

        _assert_reformed_shape(text)
        assert text.split("\n")[0] == "# Egress status"
        assert text.split("\n")[1] == ""
        assert text.split("\n")[2] == "- Enabled: `yes`"
        assert "- Binary: `/usr/bin/egress`" in text
        assert "- Process: `pid 1234`" in text
        assert "## Token mappings" in text
        assert "- `OPENAI_API_KEY`: `tok_...1234` (api.openai.com)" in text
        assert "## Uncovered providers" in text
        assert "- `anthropic`" in text
        assert "## Next step" in text
        assert "hermes egress setup" in text
        assert "Egress proxy status" not in text, "flat title line is gone"

    def test_status_omits_absent_sections(self, monkeypatch):
        from hermes_cli.proxy_cli import format_status_text

        self._stub_proxy(monkeypatch)

        text = format_status_text()

        _assert_reformed_shape(text)
        assert "## Token mappings" not in text
        assert "## Uncovered providers" not in text
        assert "## Next step" not in text
        assert "- Scope: Docker backend only in this release" in text
