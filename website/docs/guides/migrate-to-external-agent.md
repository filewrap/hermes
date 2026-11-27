---
sidebar_position: 11
title: "Migrate to External Agent"
description: "Complete guide to exporting your Hermes setup to External Agent — what gets exported, how config maps, and what to check after."
---

# Migrate to External Agent

`hermes export-agent external-agent` exports your Hermes setup into a fresh External Agent workspace. This guide covers exactly what gets exported, the config key mappings, and what to verify after migration.

This is the reverse of [Migrate from External Agent](./migrate-from-external-agent.md) (`hermes import-agent external-agent`, from the `agent-importers` plugin). The export always shows a full preview of what will be written before making any changes. Review the plan, then confirm to proceed.

## Quick start

```bash
# Preview only, no changes
hermes export-agent external-agent --dry-run

# Preview, confirm, export into ./agent-export-<agent>-<timestamp>
hermes export-agent external-agent

# Export into a chosen directory, skip confirmation, also write a .zip
hermes export-agent external-agent --out ~/agent-export --yes --zip
```

Reads from the active Hermes profile home by default. Pass `--source <path>` when the home lives elsewhere, `--agent-id <id>` to name the External Agent agent (default: `main` for the default profile).

Secrets are never copied. API keys, bot tokens, and MCP credentials leave as `${VAR}` references, and `.env.example` lists every referenced variable empty. Fill the values in on the External Agent side.

## Options

| Option | Description |
|--------|-------------|
| `--dry-run` | Preview only — stop after showing what would be exported. |
| `--source <path>` | Hermes home to export (default: the active profile home). |
| `--out <path>` | Output directory (default: `./agent-export-<agent>-<timestamp>`). |
| `--agent-id <id>` | External Agent agent id (default: `main` for the default profile). |
| `--overwrite` | Replace existing files on conflicts (default: refuse to write them). |
| `--zip` | Also write the export as a `.zip` beside the directory. |
| `--yes` | Skip the confirmation prompt after preview. |

## What gets exported

### Persona, memory, and instructions

| What | Hermes source | External Agent destination | Notes |
|------|--------------|---------------------|-------|
| Persona | `SOUL.md` | `agents/<id>/SOUL.md` | Direct copy |
| Workspace instructions | `AGENTS.md` | `agents/<id>/AGENTS.md` | Direct copy |
| Identity | `IDENTITY.md` | `agents/<id>/IDENTITY.md` | Direct copy |
| Long-term memory | `memories/MEMORY.md` | `agents/<id>/MEMORY.md` | Direct copy |
| User profile | `memories/USER.md` | `agents/<id>/USER.md` | Direct copy |
| Tool notes | `TOOLS.md` | `agents/<id>/TOOLS.md` | Direct copy, when present |

### Skills

| Source | Hermes location | External Agent destination |
|--------|----------------|---------------------|
| Profile skills | `<home>/skills/` | `agents/<id>/skills/` |

Every directory containing a `SKILL.md` is copied whole, including `references/`, `templates/`, `assets/`, and `scripts/`. Tooling caches (`.venv`, `node_modules`, `__pycache__`, …) and inactive `_org` mirrors are skipped. Skill conflicts (two directories normalizing to one name) are reported in the plan; re-run with `--overwrite` to replace a conflicting target.

Skill config maps too: `config.yaml` → `skills.config` becomes `openclaw.json` → `skills.entries`, and `skills.disabled` becomes `enabled: false` entries (Hermes always keeps its own operating manual active, so `hermes-agent` is never disabled).

### Model and provider configuration

| What | Hermes source | External Agent destination | Notes |
|------|--------------|---------------------|-------|
| Default model | `config.yaml` → `model` | `openclaw.json` → `agents.defaults.model.primary` | `provider/model` form; `model` can be a string or `{default, provider}` |
| Fallback models | `config.yaml` → `model.fallbacks` | `agents.defaults.model.fallbacks` | When stated |
| Providers | `config.yaml` → `providers.*` + `custom_providers` | `openclaw.json` → `models.providers.*` and `agents/<id>/models.json` | Same provider entries in both files; `models.mode` is `merge` |
| Provider API keys | `config.yaml` → key env vars / `.env` | `models.providers.*.apiKey` as `"${VAR}"` | Reference only — the value is never copied |
| Custom endpoints | `providers.*.base_url` / `custom_providers` | `models.providers.*.baseUrl` | Transport maps to `api`: `chat_completions` → `openai-completions`, `anthropic_messages` → `anthropic-messages`, `codex_responses` → `openai-responses` |

Model rows are enriched from Hermes's bundled External Agent catalog when the provider+model matches (context window, token cap, cost, input modalities); unknown models keep only what Hermes states, and External Agent's catalog merge fills the rest.

### Agent behavior

| What | Hermes config path | External Agent config path | Mapping |
|------|-------------------|---------------------|---------|
| Max turns | `agent.max_turns` | `agents.defaults.timeoutSeconds` | `max_turns * 10` |
| Verbose mode | `agent.verbose` | `agents.defaults.verboseDefault` | Direct copy |
| Reasoning effort | `agent.reasoning_effort` | `agents.defaults.thinkingDefault` | `high` → `high`, `medium` → `auto`, `low` → `off` |
| Compression | `compression.enabled` | `agents.defaults.compaction.mode` | `true` → `auto`, `false` → `off` |
| Compression model | `compression.summary_model` | `agents.defaults.compaction.model` | Direct copy |
| Human delay | `human_delay.*` | `agents.defaults.humanDelay.*` | Direct copy |
| Timezone | `timezone` | `agents.defaults.userTimezone` | Direct copy |
| Exec timeout | `terminal.timeout` | `agents.defaults.tools.exec.timeoutSec` | Direct copy |
| Docker sandbox | `terminal.backend` / `terminal.docker_image` | `agents.defaults.sandbox.backend` / `.docker.image` | `docker` only |
| Working directory | `terminal.cwd` | `agents.defaults.workspace` | Direct copy |

### MCP servers

| Hermes field | External Agent field | Notes |
|-------------|---------------|-------|
| `mcp_servers.*.command` | `mcp.servers.*.command` | Stdio transport |
| `mcp_servers.*.args` | `mcp.servers.*.args` | |
| `mcp_servers.*.env` | `mcp.servers.*.env` | `${VAR}` refs pass through; credential-like literal values become `${KEY}` references |
| `mcp_servers.*.headers` | `mcp.servers.*.headers` | Same rule as `env` |
| `mcp_servers.*.url` | `mcp.servers.*.url` | HTTP/SSE transport |
| `mcp_servers.*.tools.include` / `.exclude` | `mcp.servers.*.toolFilter` | Exact names and `*` only; `?` and bracket patterns are dropped and reported |
| `mcp_servers.*.oauth` | `mcp.servers.*.oauth` | `authProfileId`, `scope`, `redirectUrl`, `clientMetadataUrl`; re-login with `openclaw mcp login <name>` |

Reads `mcp_servers` first, then `mcp.servers`, then a bare `mcp` mapping.

### TTS (text-to-speech)

| What | External Agent destination |
|------|---------------------|
| Provider name | `messages.tts.provider` |
| ElevenLabs voice ID / model ID | `messages.tts.providers.elevenlabs.voiceId` / `.modelId` |
| OpenAI model / voice | `messages.tts.providers.openai.model` / `.voice` |
| Edge TTS voice | `messages.tts.providers.microsoft.voice` |

### Messaging platforms

| Platform | Hermes `.env` variable | External Agent config path | Notes |
|----------|----------------------|---------------------|-------|
| Telegram | `TELEGRAM_BOT_TOKEN` | `channels.telegram.botToken` | `"${TELEGRAM_BOT_TOKEN}"` — reference only |
| Discord | `DISCORD_BOT_TOKEN` | `channels.discord.token` | Reference only |
| Slack | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `channels.slack.botToken` / `.appToken` | Reference only |
| Matrix | `MATRIX_ACCESS_TOKEN` | `channels.matrix.accessToken` | Reference only |
| Mattermost | `MATTERMOST_BOT_TOKEN` | `channels.mattermost.botToken` | Reference only |

Channel allowlists are not exported: Hermes stores them per-adapter and External Agent reads them from `credentials/<platform>-default-allowFrom.json`. Recreate each channel's allowlist in External Agent after the gateway starts.

### Other config

| What | Hermes path | External Agent path | Notes |
|------|------------|--------------|-------|
| Approval mode | `approvals.mode` | `approvals.exec.mode` | `off` → `auto`, `manual` → `always`, `smart` → `smart` |
| Command allowlist | `command_allowlist` | `exec-approvals.json` (beside `openclaw.json`) | `{ "allow": [...] }` |
| Browser CDP URL | `browser.cdp_url` | `browser.cdpUrl` | |
| Browser headless | `browser.headless` | `browser.headless` | |
| Memory plugin slot | memory files present, or `memory.provider` set | `plugins.slots.memory: "memory-core"` | |
| Honcho memory | `memory.provider: honcho` | `plugins.entries.honcho` (manual activation) | Reported as a manual item |

## Archived (no live External Agent equivalent)

These are copied to `archive/` in the export for manual review and are never loaded into External Agent:

| What | Archive location | How to recreate in External Agent |
|------|-----------------|------------------------------|
| Sessions | `archive/sessions/sessions-index.json` + `state.db` snapshot | Transcripts are reference only; start fresh sessions in External Agent |
| Cron jobs | `archive/cron/jobs.json` + `cron-jobs.md` | Recreate with External Agent's scheduler |
| Plugins | `archive/plugins/` + `plugins-index.json` | Install the External Agent equivalent, if one exists |
| Other Hermes state | `archive/hermes-state/` | Manual review |

The `state.db` snapshot is a self-contained SQLite copy (`VACUUM INTO`, chmod 0600). It may contain secrets that were inside sessions — keep it local and delete it when you no longer need it.

## After migration

1. **Check the export report** — `export-report.json` in the output directory, with counts of written, skipped, and conflicting items plus every manual-review item.
2. **Copy the tree into your External Agent home** — the output directory is a fresh External Agent home (`openclaw.json` at the root, `agents/<id>/` below it).
3. **Fill in `.env.example`** — copy it to `.env` and set every referenced variable, or set them in your shell.
4. **Run `openclaw doctor`** — it validates the config before the gateway starts.
5. **Start a private session** — test a harmless MCP server and search memory for one known imported fact.
6. **Only then connect channels or recreate cron jobs.**

## Troubleshooting

### "No Hermes state found"

The export checks the active profile home, then the path from `--source`. If your installation lives elsewhere, use `--source /path/to/your/hermes-home`.

### "No default model found"

The Hermes config states no default model, so `agents.defaults.model` was left unset. Pick a model in External Agent after the move (`openclaw models set <provider/model>`).

### "Secret value found in live file"

The post-write scan found a harvested secret value in an emitted live file and failed the export. The report names the file, never the value. Remove the literal secret from the Hermes source (move it to `.env` as a `${VAR}` reference) and re-run.

### Retired Qwen providers

Hermes Qwen Portal OAuth and Qwen CLI credentials cannot be reused by External Agent. Authenticate qwen with an API key after migration: `openclaw onboard --auth-choice qwen-api-key`.

### Skills not appearing after migration

Imported skills land in `agents/<id>/skills/`. Restart the gateway for them to take effect, or check the export report for skill conflicts.
