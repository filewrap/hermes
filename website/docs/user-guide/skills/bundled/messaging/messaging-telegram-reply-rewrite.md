---
title: "Telegram Reply Rewrite — Rewrite agent replies for a non-engineer on a phone"
sidebar_label: "Telegram Reply Rewrite"
description: "Rewrite agent replies for a non-engineer on a phone"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Telegram Reply Rewrite

Rewrite agent replies for a non-engineer on a phone.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/messaging/telegram-reply-rewrite` |
| Version | `1.0.0` |
| Author | serejaris, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `telegram`, `rich-messages`, `messaging` |
| Related skills | [`telegram-rich-messages`](../../bundled/messaging/messaging-telegram-rich-messages.md), [`telegram-markdown-to-rich`](../../bundled/messaging/messaging-telegram-markdown-to-rich.md), [`telegram-client`](../../bundled/messaging/messaging-telegram-client.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Telegram Reply Rewrite Skill

Rewrite a raw agent reply — a technical log, a plan, or a status update — into a short, scannable Telegram rich message for a non-engineer reading on a phone. The user reads the first line in the notification; the rest must be skimmable without scrolling. This skill is a port of `tg-reply-rewrite` (serejaris/telegram-skills, Bot API 10.2) to Telethon layer 233. It is guidance only; it has no code.

## When to Use

Use when the user asks to rewrite a reply, summarize a status, or explain a result to someone who is not an engineer. Use it for the reply that goes out in the chat; use the rich-messages skill when the reply needs tables or a folded-out section. Do not use it to hide facts — a rewrite that drops a fact is a lie, not a summary.

## Prerequisites

- The original reply text. Every fact, number, identifier, and date in it must survive the rewrite character-for-character.
- `telegram_client` is available in this Telegram conversation when the rewritten reply is sent. `rich_message_send` and `quote_reply` are the only actions an admitted human may call directly; both always target the active profile/chat/topic and cannot be redirected.

## How to Run

1. **Read the original reply fully.** Pull out the facts: what happened, what it means, what the reader should do.
2. **Rewrite in structure, not in meaning.** Follow the structure below. Keep every fact, number, identifier, and date exactly as written.
3. **Send the rewrite** with `telegram_client` `action="rich_message_send"` (`format: "html"`) or `action="quote_reply"` when it answers a specific message. Plain text is fine for one line; anything longer goes as a rich message.

## Quick Reference

### Structure

- **First line: what happened + why it matters.** ~80 characters, max 120. This is what the notification shows.
- **Then the findings.** One thought per block, in this order:
  - *What happened* — the fact, in one block.
  - *What it means* — the consequence, in one block.
  - *What to do* — a concrete action, with a copyable sentence, in one block.
- **Keep the essential details** — the ones that change the next decision.
- **Explain or remove every jargon term.** No unexplained acronyms.

### Length budgets

| Limit | Value |
| --- | --- |
| First line | ~80 characters, max 120 |
| Findings shown open | 5 maximum |
| Whole message | aim under 1,200 characters |

### Tools

| Need | Tool |
| --- | --- |
| A collapsed section for details | `<details>` blocks (see the rich-messages skill) |
| A comparison | `<table>` blocks (see the rich-messages skill) |
| A short reply as HTML | `rich_message_send`, `format: "html"` |
| A reply to a specific message | `quote_reply` |

## Procedure

### Example

Original:

```
The deployment failed with a timeout during the database migration. The
connection pool was exhausted because the previous migration left 40
connections open. The fix is to restart the service and re-run the
migration with a smaller batch size.
```

Rewritten:

```
Deployment failed — the database migration timed out.

What happened: the migration ran out of database connections. The previous
migration left 40 connections open.

What it means: the new code is not live yet, and the old version is still
serving traffic.

What to do: restart the service, then re-run the migration in smaller
batches. Copy: `restart service; migrate --batch 100`
```

### Secrets

Replace a secret, token, or credential in the original with `[removed]` — never copy it into the rewrite, and never print it. This applies to the reply, the logs, and any quoted text.

## Pitfalls

- Never change a fact, a number, an identifier, or a date. If the original says 40 connections, the rewrite says 40 connections.
- Never invent a next step the original did not imply. If there is no action, say so.
- Never pad a short reply to fill the structure. One line is a fine reply.
- A rewrite that drops the "what to do" block leaves the reader waiting. If the action is unknown, say "No action needed from you" explicitly.

## Verification

- Read the rewrite back against the original: every fact, number, identifier, and date matches character-for-character.
- The first line reads as a complete sentence under 120 characters.
- No jargon term survives unexplained; no secret survives at all.
- The message sends as one rich message (or one plain line) and reads cleanly on a phone screen.
