"""Behaviour contracts for the reverse migration (Hermes -> External Agent).

Builds synthetic Hermes homes in tmp_path and drives the exporter
through its public entry points: plan, apply, validate, and the CLI
handler. Secrets here are planted fakes; the suite asserts they never
reach a live exported file.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.export_agent import (
    ExportOptions,
    apply_export_plan,
    build_export_plan,
    export_external_agent,
)
from hermes_cli.export_agent_config import build_export_config
from hermes_cli.export_agent_model import (
    collect_hermes_providers,
    enrich_model_row,
    resolve_hermes_model_ref,
)
from hermes_cli.export_agent_validate import validate_export_tree


CONFIG = """\
model:
  default: "anthropic/claude-opus-4.8"
  provider: "anthropic"
providers:
  anthropic:
    base_url: "https://api.anthropic.com"
    models:
      - "claude-opus-4.8"
  openrouter:
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: "OPENROUTER_API_KEY"
    models:
      - "openai/gpt-5.4"
mcp_servers:
  fetch:
    command: "uvx"
    args: ["mcp-server-fetch"]
    env:
      FETCH_API_KEY: "sk-live-PLANTEDSECRET0001"
      FETCH_MODE: "fast"
agent:
  max_turns: 50
  reasoning_effort: "high"
timezone: "Asia/Kolkata"
compression:
  enabled: true
  summary_model: "anthropic/claude-haiku-4.5"
approvals:
  mode: "smart"
command_allowlist:
  - "ls *"
  - "git status"
skills:
  disabled: ["old-skill"]
"""

ENV = """\
ANTHROPIC_API_KEY=sk-ant-PLANTEDSECRET0002
OPENROUTER_API_KEY=sk-or-PLANTEDSECRET0003
TELEGRAM_BOT_TOKEN=123456:PLANTEDBOTTOKEN0004
"""

PLANTED_SECRETS = (
    "sk-live-PLANTEDSECRET0001",
    "sk-ant-PLANTEDSECRET0002",
    "sk-or-PLANTEDSECRET0003",
    "123456:PLANTEDBOTTOKEN0004",
)


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    """A synthetic Hermes home with config, .env, workspace, skill, cron."""
    home = tmp_path / "hermes"
    (home / "skills" / "demo" / "references").mkdir(parents=True)
    (home / "memories").mkdir()
    (home / "cron").mkdir()
    (home / "plugins" / "some-plugin").mkdir(parents=True)
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (home / ".env").write_text(ENV, encoding="utf-8")
    (home / "SOUL.md").write_text("# Soul\nBe helpful.\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("# Agents\nOperating manual.\n", encoding="utf-8")
    (home / "IDENTITY.md").write_text("# Identity\nmusie\n", encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text(
        "# Memory\nRemembers.\n", encoding="utf-8"
    )
    (home / "memories" / "USER.md").write_text("# User\nkailashhh\n", encoding="utf-8")
    (home / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill for export testing.\n---\n# Demo\n",
        encoding="utf-8",
    )
    (home / "skills" / "demo" / "references" / "notes.md").write_text(
        "reference\n", encoding="utf-8"
    )
    (home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "daily-digest",
                        "name": "Daily digest",
                        "enabled": True,
                        "schedule": {"kind": "cron", "cron": "0 9 * * *"},
                        "prompt": "Summarize the day.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (home / "plugins" / "some-plugin" / "plugin.json").write_text(
        '{"name": "some-plugin"}', encoding="utf-8"
    )
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, model TEXT,"
        " started_at INTEGER, last_active INTEGER, message_count INTEGER, hidden INTEGER, archived INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('sess-1', 'Test session', 'cli',"
        " 'anthropic/claude-opus-4.8', 1762800000, 1762803600, 12, 0, 0)"
    )
    conn.commit()
    conn.close()
    return home


def _plan(hermes_home: Path, tmp_path: Path, **kwargs):
    return build_export_plan(
        ExportOptions(
            source=str(hermes_home), out=str(tmp_path / "out"), yes=True, **kwargs
        )
    )


def _live_text(out_dir: Path) -> str:
    """All live (non-archive) exported text, for secret-leak assertions."""
    chunks = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_file() and "archive" not in path.relative_to(out_dir).parts:
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                pass
    return "\n".join(chunks)


def test_model_ref_resolves_string_and_dict_forms():
    assert (
        resolve_hermes_model_ref({"model": "claude-opus-4.8", "provider": "anthropic"})
        == "anthropic/claude-opus-4.8"
    )
    assert (
        resolve_hermes_model_ref(
            {"model": {"default": "gpt-5.4", "provider": "openai"}}
        )
        == "openai/gpt-5.4"
    )
    assert (
        resolve_hermes_model_ref(
            {"model": {"default": "gemini-3-pro", "provider": "gemini"}}
        )
        == "google/gemini-3-pro"
    )
    assert resolve_hermes_model_ref({}) is None


def test_provider_collection_emits_key_env_references_not_values(hermes_home: Path):
    from hermes_cli.export_agent import read_source_config

    config, env = read_source_config(hermes_home)
    providers, _manual = collect_hermes_providers(config, env)
    by_id = {entry.id: entry for entry in providers}
    assert by_id["anthropic"].api == "anthropic-messages"
    assert by_id["anthropic"].api_key_env == "ANTHROPIC_API_KEY"
    assert by_id["openrouter"].api == "openai-completions"
    assert by_id["openrouter"].base_url == "https://openrouter.ai/api/v1"


def test_catalog_enrichment_normalizes_dots_to_dashes():
    row = enrich_model_row("anthropic", {"id": "claude-opus-4.8"})
    assert row.get("reasoning") is True
    assert row.get("contextWindow")
    unknown = enrich_model_row("anthropic", {"id": "claude-no-such-model-9.9"})
    assert "contextWindow" not in unknown


def test_plan_covers_all_sources_and_references_secrets(
    hermes_home: Path, tmp_path: Path
):
    plan, _config, _env, _doc = _plan(hermes_home, tmp_path)
    ids = {item.id for item in plan.items}
    assert "config:openclaw.json" in ids
    assert "config:models.json" in ids
    assert "workspace:SOUL.md" in ids
    assert "memory:MEMORY.md" in ids
    assert "skill:demo" in ids
    assert "archive:cron" in ids
    assert "archive:state.db" in ids
    assert "secret-ref:ANTHROPIC_API_KEY" in ids
    assert "secret-ref:TELEGRAM_BOT_TOKEN" in ids
    manual = [item for item in plan.items if item.kind == "manual"]
    assert any("FETCH_API_KEY" in (item.message or "") for item in manual)
    assert "ANTHROPIC_API_KEY" in plan.env_vars
    assert "TELEGRAM_BOT_TOKEN" in plan.env_vars


def test_apply_writes_matching_external_agent_tree(hermes_home: Path, tmp_path: Path):
    plan, config, env, document = _plan(hermes_home, tmp_path)
    report = apply_export_plan(
        plan,
        config,
        env,
        document,
        options=ExportOptions(source=str(hermes_home), out=str(tmp_path / "out")),
    )
    out_dir = tmp_path / "out"
    assert report.validated
    assert not report.validation_errors
    exported = json.loads((out_dir / "openclaw.json").read_text(encoding="utf-8"))
    assert (
        exported["agents"]["defaults"]["model"]["primary"]
        == "anthropic/claude-opus-4.8"
    )
    assert exported["agents"]["defaults"]["timeoutSeconds"] == 500
    assert exported["agents"]["defaults"]["thinkingDefault"] == "high"
    assert exported["agents"]["defaults"]["userTimezone"] == "Asia/Kolkata"
    assert (
        exported["models"]["providers"]["anthropic"]["apiKey"] == "${ANTHROPIC_API_KEY}"
    )
    assert (
        exported["models"]["providers"]["openrouter"]["apiKey"]
        == "${OPENROUTER_API_KEY}"
    )
    assert exported["channels"]["telegram"]["botToken"] == "${TELEGRAM_BOT_TOKEN}"
    assert (
        exported["mcp"]["servers"]["fetch"]["env"]["FETCH_API_KEY"]
        == "${FETCH_API_KEY}"
    )
    assert exported["mcp"]["servers"]["fetch"]["env"]["FETCH_MODE"] == "fast"
    assert exported["skills"]["entries"]["old-skill"]["enabled"] is False
    models = json.loads(
        (out_dir / "agents" / "main" / "models.json").read_text(encoding="utf-8")
    )
    assert set(models["providers"]) == set(exported["models"]["providers"])
    assert (out_dir / "agents" / "main" / "SOUL.md").read_bytes() == (
        hermes_home / "SOUL.md"
    ).read_bytes()
    assert (
        out_dir / "agents" / "main" / "skills" / "demo" / "references" / "notes.md"
    ).is_file()
    env_example = (out_dir / ".env.example").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=" in env_example
    assert "TELEGRAM_BOT_TOKEN=" in env_example
    approvals = json.loads(
        (out_dir / "exec-approvals.json").read_text(encoding="utf-8")
    )
    assert approvals["allow"] == ["git status", "ls *"]


def test_no_planted_secret_reaches_a_live_file(hermes_home: Path, tmp_path: Path):
    plan, config, env, document = _plan(hermes_home, tmp_path)
    apply_export_plan(
        plan,
        config,
        env,
        document,
        options=ExportOptions(source=str(hermes_home), out=str(tmp_path / "out")),
    )
    live = _live_text(tmp_path / "out")
    for secret in PLANTED_SECRETS:
        assert secret not in live


def test_archive_holds_sessions_cron_and_plugins_only(
    hermes_home: Path, tmp_path: Path
):
    plan, config, env, document = _plan(hermes_home, tmp_path)
    apply_export_plan(
        plan,
        config,
        env,
        document,
        options=ExportOptions(source=str(hermes_home), out=str(tmp_path / "out")),
    )
    out_dir = tmp_path / "out"
    index = json.loads(
        (out_dir / "archive" / "sessions" / "sessions-index.json").read_text(
            encoding="utf-8"
        )
    )
    assert [s["id"] for s in index["sessions"]] == ["sess-1"]
    assert (out_dir / "archive" / "sessions" / "state.db").is_file()
    cron_readme = (out_dir / "archive" / "cron" / "cron-jobs.md").read_text(
        encoding="utf-8"
    )
    assert "Daily digest" in cron_readme
    assert "NOT recreated" in cron_readme
    assert (
        json.loads(
            (out_dir / "archive" / "cron" / "jobs.json").read_text(encoding="utf-8")
        )["jobs"][0]["id"]
        == "daily-digest"
    )
    assert (out_dir / "archive" / "plugins" / "some-plugin" / "plugin.json").is_file()
    exported = json.loads((out_dir / "openclaw.json").read_text(encoding="utf-8"))
    assert "cron" not in json.dumps(exported)
    assert "daily-digest" not in json.dumps(exported)


def test_validation_fails_when_a_literal_secret_would_ship(
    hermes_home: Path, tmp_path: Path
):
    # A literal MCP env value under a non-credential key that equals a
    # harvested secret must fail validation and name the file, not the value.
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'FETCH_MODE: "fast"', 'FETCH_MODE: "sk-ant-PLANTEDSECRET0002"'
        ),
        encoding="utf-8",
    )
    plan, config, env, document = _plan(hermes_home, tmp_path)
    report = apply_export_plan(
        plan,
        config,
        env,
        document,
        options=ExportOptions(source=str(hermes_home), out=str(tmp_path / "out")),
    )
    assert not report.validated
    assert any("openclaw.json" in error for error in report.validation_errors)
    assert not any(
        secret in error
        for secret in PLANTED_SECRETS
        for error in report.validation_errors
    )


def test_dry_run_writes_nothing(hermes_home: Path, tmp_path: Path, capsys):
    out_dir = tmp_path / "out"
    code = export_external_agent(
        ExportOptions(source=str(hermes_home), out=str(out_dir), dry_run=True)
    )
    assert code == 0
    assert not out_dir.exists()
    assert "Dry run" in capsys.readouterr().out


def test_conflict_without_overwrite_then_overwrite(hermes_home: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    first = export_external_agent(
        ExportOptions(source=str(hermes_home), out=str(out_dir), yes=True)
    )
    assert first == 0
    (hermes_home / "SOUL.md").write_text("# Soul\nChanged.\n", encoding="utf-8")
    plan, _config, _env, _doc = _plan(hermes_home, tmp_path)
    assert any(item.status == "conflict" for item in plan.items)
    second = export_external_agent(
        ExportOptions(source=str(hermes_home), out=str(out_dir), yes=True)
    )
    assert second == 0
    assert (out_dir / "agents" / "main" / "SOUL.md").read_text(
        encoding="utf-8"
    ) == "# Soul\nBe helpful.\n"
    third = export_external_agent(
        ExportOptions(
            source=str(hermes_home), out=str(out_dir), yes=True, overwrite=True
        )
    )
    assert third == 0
    assert (out_dir / "agents" / "main" / "SOUL.md").read_text(
        encoding="utf-8"
    ) == "# Soul\nChanged.\n"


def test_retired_qwen_provider_is_a_manual_item(hermes_home: Path, tmp_path: Path):
    (hermes_home / "config.yaml").write_text(
        'model:\n  default: "qwen-portal/qwen3-coder"\n  provider: "qwen-portal"\n',
        encoding="utf-8",
    )
    plan, _config, _env, _doc = _plan(hermes_home, tmp_path)
    assert any(item.id == "manual:auth-reauthenticate:qwen" for item in plan.items)


def test_missing_home_errors_cleanly(tmp_path: Path, capsys):
    code = export_external_agent(
        ExportOptions(source=str(tmp_path / "nope"), out=str(tmp_path / "out"))
    )
    assert code == 1
    assert "No Hermes state found" in capsys.readouterr().out


def test_external_agent_config_mapping_round_trip():
    document, _vars = build_export_config(
        {
            "agent": {"max_turns": 20, "reasoning_effort": "low"},
            "compression": {"enabled": False},
            "approvals": {"mode": "manual"},
            "browser": {"headless": True},
        },
        model_ref="openai/gpt-5.4",
        fallbacks=["anthropic/claude-opus-4.8"],
        providers={
            "openai": {
                "baseUrl": "https://api.openai.com/v1",
                "api": "openai-responses",
                "models": [
                    {"id": "gpt-5.4", "name": "gpt-5.4", "api": "openai-responses"}
                ],
            }
        },
        mcp_servers={},
        env={},
        has_memory_files=True,
    )
    assert document["agents"]["defaults"]["timeoutSeconds"] == 200
    assert document["agents"]["defaults"]["thinkingDefault"] == "off"
    assert document["agents"]["defaults"]["compaction"]["mode"] == "off"
    assert document["agents"]["defaults"]["model"]["fallbacks"] == [
        "anthropic/claude-opus-4.8"
    ]
    assert document["approvals"]["exec"]["mode"] == "always"
    assert document["browser"]["headless"] is True
    assert document["plugins"]["slots"]["memory"] == "memory-core"


def test_cli_parser_registers_export_agent():
    import argparse

    from hermes_cli.subcommands.agent_export import build_export_agent_parser
    from hermes_cli.export_agent import cmd_export_agent

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_export_agent_parser(subparsers)
    args = parser.parse_args(
        ["export-agent", "external-agent", "--source", "/tmp/x", "--dry-run", "--zip"]
    )
    assert args.func is cmd_export_agent
    assert args.source == "/tmp/x"
    assert args.dry_run is True
    assert args.zip is True
    assert args.export_agent == "external-agent"


def test_validate_tree_rejects_unknown_default_provider(tmp_path: Path):
    out_dir = tmp_path / "out"
    (out_dir / "agents" / "main").mkdir(parents=True)
    (out_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": {"primary": "nope/model-x"}}},
                "models": {"mode": "merge", "providers": {}},
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "agents" / "main" / "models.json").write_text(
        json.dumps({"providers": {}}), encoding="utf-8"
    )
    errors = validate_export_tree(out_dir, source_files={}, secret_values=[])
    assert any("nope" in error for error in errors)
