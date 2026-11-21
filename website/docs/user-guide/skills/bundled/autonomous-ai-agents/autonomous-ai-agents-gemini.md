---
title: "Gemini — One-shot prompts, summaries, and generation via the CLI"
sidebar_label: "Gemini"
description: "One-shot prompts, summaries, and generation via the CLI"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Gemini

One-shot prompts, summaries, and generation via the CLI.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/autonomous-ai-agents/gemini` |
| Version | `1.0.0` |
| Author | External Agent contributors, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `gemini`, `google`, `cli`, `prompts`, `generation`, `mcp` |
| Related skills | [`claude-code`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code.md), [`codex`](../../bundled/autonomous-ai-agents/autonomous-ai-agents-codex.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Gemini Skill

Use the Gemini CLI in headless one-shot mode: prompts, summaries, generation, skills, hooks, MCP, or Gemma routing. Ported from External Agent `skills/gemini/SKILL.md`, adapted to Hermes `terminal` calls.

## When to Use

- A one-shot Gemini answer, summary, or generation where a shell-out is the right tool.
- Managing Gemini extensions, skills, hooks, or MCP servers from the command line.
- Do not use for interactive Gemini sessions — use `terminal` with `pty=true` for those, or a Hermes-native workflow instead.

## Prerequisites

- The `gemini` CLI installed on the host (`npm install -g @google/gemini-cli`, needs Node.js 20+). Not bundled with Hermes; the host owner installs it.
- Auth: a paid Gemini API key (`GEMINI_API_KEY`), Vertex AI, or a Gemini Code Assist Standard/Enterprise license. Since 2026-06-18 the CLI no longer serves personal Google accounts or the free tier — a plain Google login does not work. If auth fails, check the key/license first rather than working around it.
- Check the install with `terminal(command="gemini --version")`.

## How to Run

One-shot mode with `-p/--prompt`. Positional text starts interactive mode, so always pass `-p` for non-interactive use. Run everything through `terminal`.

## Quick Reference

| Need | Command |
|---|---|
| One-shot prompt | `gemini -p "Answer this question..."` |
| Pick a model | `gemini -m <model> -p "Prompt..."` |
| JSON output | `gemini -p "Return JSON" --output-format json` |
| Summarize a file | `cat notes.md \| gemini -p "Summarize"` |
| List extensions | `gemini --list-extensions` |
| Extensions / skills / hooks / MCP | `gemini extensions <command>`, `gemini skills <command>`, `gemini hooks <command>`, `gemini mcp <command>` |

## Procedure

### One-shot prompts

```
terminal(command="gemini -p \"Answer this question...\"")
terminal(command="gemini -m <model> -p \"Prompt...\"")
terminal(command="gemini -p \"Return JSON\" --output-format json")
```

Stdin appends to `-p`, so pipe content in for summarization:

```
terminal(command="cat notes.md | gemini -p \"Summarize\"")
```

### Extensions, skills, hooks, MCP

```
terminal(command="gemini --list-extensions")
terminal(command="gemini extensions <command>")
terminal(command="gemini skills <command>")
terminal(command="gemini hooks <command>")
terminal(command="gemini mcp <command>")
```

## Pitfalls

- Forgetting `-p` drops into interactive mode and the call hangs — always use `-p` for one-shot work, and prefer a bounded `timeout` on the `terminal` call for long generations.
- Auth failures mean the API key/license is missing, expired, or a personal Google login is being used (unsupported since 2026-06-18): fix the credential rather than working around it.

## Verification

- `terminal(command="gemini --version")` prints a version, and a one-shot `gemini -p "Say hi"` returns text.
- Live check on the host: the CLI is installed and logged in, and a one-shot prompt returns an answer. The CLI and its auth are host-side prerequisites; without them the skill reports the missing setup instead of working around it.
