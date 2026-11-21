---
name: external-agent-export
description: "Export a Hermes home into an External Agent workspace."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [external-agent, export, migration, reverse-migration]
    category: migration
    related_skills: []
---

# External Agent Export Skill

Export the active Hermes home into a fresh External Agent workspace: model and
providers, MCP servers, workspace files, memory files, and skills become
`openclaw.json` plus `agents/<id>/`. Plugins, sessions, and cron jobs are
archived for manual review, never activated. Secrets are never copied —
keys and tokens leave as `${VAR}` references only.

This skill drives `hermes export-agent external-agent`. It does not move files
itself and never reads credential values.

## When to Use

- The user wants to move a Hermes setup to External Agent, or keep an
  External Agent copy of it as a fallback.
- The user asks what would move, what would not, or whether a Hermes
  home can run under External Agent.
- After an export, when the user asks what is still missing or what to
  do next in External Agent.

Do not use it to import an External Agent setup into Hermes — that is the
`agent-importers` plugin (`hermes import-agent external-agent`).

## Prerequisites

- Hermes CLI on the machine that holds the Hermes home to export.
- The export writes into a new directory (default
  `./agent-export-<agent>-<timestamp>`); the source home is only read.
- Nothing here needs network access or model calls beyond the agent's own
  turn. The export never contacts External Agent.

## How to Run

Always preview first. Run the dry run, read the plan back to the user,
and only apply after they confirm.

```bash
hermes export-agent external-agent --dry-run
hermes export-agent external-agent --out ~/agent-export --yes
```

Useful flags: `--source <path>` when the Hermes home lives outside the
active profile, `--agent-id <id>` for the External Agent agent id,
`--overwrite` to replace conflicting targets, `--zip` to also write a
`.zip` beside the directory.

## Quick Reference

| Output | Contents |
| ------ | -------- |
| `openclaw.json` | Default model, providers, MCP servers, agent behaviour, TTS, approvals, browser, channel token references. |
| `agents/<id>/models.json` | Per-agent provider catalog, same providers as `openclaw.json`. |
| `agents/<id>/SOUL.md`, `AGENTS.md`, `IDENTITY.md`, `MEMORY.md`, `USER.md`, `TOOLS.md` | Copied verbatim when present. |
| `agents/<id>/skills/` | Every discovered skill, copied whole. |
| `.env.example` | Referenced variables, empty. Fill in before starting External Agent. |
| `exec-approvals.json` | Command allowlist, when configured. |
| `archive/sessions/` | Session index plus a `state.db` snapshot. |
| `archive/cron/` | Verbatim `jobs.json` plus a readable list. Jobs are not recreated. |
| `archive/plugins/` | Installed plugin index and files. Plugins do not run under External Agent. |
| `archive/hermes-state/` | Other Hermes-only state files. |
| `export-report.json`, `README.md` | What moved, manual items, next steps. |

## Procedure

1. Run `hermes export-agent external-agent --dry-run` (add `--source` when the
   home is not the active profile).
2. Read the plan: counts, warnings, and every manual-review item. Tell
   the user which providers, MCP servers, and skills move, and which
   items need them by hand (inline keys, unresolved endpoints, retired
   Qwen credentials, channel allowlists).
3. Confirm the target directory with the user, then run the export with
   `--yes` (or let them confirm at the prompt).
4. Read back the summary: written/skipped/conflict/error counts, the
   environment variables to set, and the validation line. If validation
   failed, the export is not done — read `export-report.json` for the
   failing files and fix the source, then re-run.
5. Hand the user the next steps from the report: copy the tree into the
   External Agent home, fill in `.env.example`, run `openclaw doctor`, start a
   private session, test one harmless MCP server, then connect channels
   and recreate the cron jobs they still want.

## Pitfalls

- **Secrets never move.** If the user asks for keys or tokens to be
  copied over, refuse that part: the export emits `${VAR}` references
  and `.env.example` only. They set the values in External Agent themselves.
- **Archive is not live config.** Sessions, cron jobs, and plugins sit
  under `archive/` and External Agent never loads them. A cron job that mattered
  must be recreated by hand in External Agent's scheduler.
- **Retired Qwen providers cannot be reused.** OAuth and CLI credentials
  for `qwen-cli`, `qwen-oauth`, and `qwen-portal` need a fresh API-key
  auth in External Agent (`openclaw onboard --auth-choice qwen-api-key`).
- **The archive snapshot may hold secrets.** `archive/sessions/state.db`
  is a copy of the live session database, chmod 0600. Tell the user to
  keep it local and delete it when they no longer need it.
- **A Hermes home with no default model exports without one.**
  `agents.defaults.model` is left unset and the user picks a model in
  External Agent after the move.
- **Do not edit the export by hand to "fix" a conflict.** Re-run with
  `--overwrite` after reviewing what the conflict is, or pick a fresh
  `--out` directory.

## Verification

After an export, confirm all of these before telling the user it is done:

- `export-report.json` says `"validated": true` and lists no
  `validation_errors`.
- `openclaw.json` parses and `agents.defaults.model.primary` names a
  provider present in `models.providers`.
- `agents/<id>/models.json` lists the same providers as `openclaw.json`.
- No secret value appears in any live file — the export's own scan
  already checks this; a failure names the file, never the value.
- Workspace files in `agents/<id>/` are byte-identical to their Hermes
  sources (the validator checks this too).
- The user has the list of environment variables to set and the manual
  items from the report.
