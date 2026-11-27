---
name: telegram-rich-streaming
description: Stream LLM replies as animated Telegram rich drafts.
version: 1.0.0
author: serejaris, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [telegram, rich-messages, streaming, messaging]
    category: messaging
    related_skills: [telegram-rich-messages, telegram-client]
---

# Telegram Rich Streaming Skill

Stream LLM or AI-generated replies to Telegram in real time with native draft animation and a thinking indicator — the same UX as ChatGPT or Claude, directly inside the bot. This skill is a port of `tg-rich-streaming` (serejaris/telegram-skills, Bot API 10.2) to Telethon layer 233. It is guidance for the streaming response already built into Hermes; it builds no transport.

## When to Use

Use when the user asks for streaming replies, a thinking indicator, or token-by-token delivery of a long AI answer in a Telegram private chat. Do not use it for group chats — groups get normal replies, no streaming. Do not use it to stream edits to an already-sent message: there is no edit-loop streaming anywhere in Hermes.

## Prerequisites

- Hermes' **Streaming response** setting (Personal settings page, owner-only, default OFF) gates the stream. Flip it on in a DM `/settings` first; with it off, replies behave as before.
- The stream runs on the **rich draft stream** (`plugins/platforms/telegram/streaming_response.py`, `ResponseStreamDraft`): a live `SetTypingRequest` draft in a private 1-1 DM, reasoning inside a `PageBlockThinking` draft, content as `InputRichMessageMarkdown`. Guest turns never stream: the guestbot draft path was removed 2026-11-07 (guestbot cannot send a typing indicator); guest turns show the Thinking… status message instead.
- Drafts are ephemeral (~30 s). The stream keeps itself alive; a generation that runs past the lifetime ends as a normal final message.

## How to Run

The pattern is four mandatory steps. Skip step 3 and the draft disappears after ~30 seconds.

1. **Open the draft with a Thinking block.** As soon as the LLM call starts, the sink sends a draft whose first block is the thinking placeholder. Reasoning goes inside the streaming response, never in the final message.
2. **Update the draft as tokens arrive.** One `random_id` per generation — the same id animates, a new id replaces. Throttle to at most one update per second (trailing edge); send a keepalive every 20 s on the 30 s TTL. Drop the Thinking block from the first update that contains real content.
3. **Finalize (mandatory).** Sync the draft before finalising: the final message must share at least one leading character with the last draft, or the draft lingers. The final lands as a normal message; the draft is gone.
4. **No edits after finalization.** Do not stream edits to the permanent message. A correction is a new message, not an edit loop.

## Quick Reference

| Rule | Value |
| --- | --- |
| Where | Private 1-1 DM, non-guest, owner chat only. Groups get normal replies. |
| Throttle | 1 s trailing edge; keepalive every 20 s; 300 s max lifetime. |
| Draft content | Reasoning → `PageBlockThinking` draft (never in the final). Content → `InputRichMessageMarkdown`, truncated at 4000 chars with fence closing. |
| Identity | One `random_id` per generation; never reuse a draft `random_id` for the final send. |
| Finalize | Sync-draft before finalising (shared leading character), then send the final as a normal message. |
| Failure | Rich rejection → plain-text fallback. `TEXTDRAFT_PEER_INVALID` → the sink disables itself. `FloodWait` extends the gate. |
| Stop | Not built. `can_stop` is deliberately left unset — there is no Stop button and no `UpdateUserTyping` handler yet. Do not promise one. |

### Guest turns vs streaming response

| | Guest turns | Streaming response |
| --- | --- | --- |
| Scope | Guest queries, everywhere | Private 1-1 DM, non-guest, owner chat |
| Mechanism | No draft; Thinking… status message, edited in place with the answer | Rich draft stream, 1 s throttle, 20 s keepalive |
| Setting | None (guest streaming removed 2026-11-07) | **Streaming response** setting (default OFF) |
| Status | Removed: the draft never worked live (0 successes, 17 `PeerIdInvalidError` failures on the host) | Built, pushed, live-test pending |

## Procedure

### Turn streaming on for a DM

1. Pull and restart Hermes on the host.
2. In the DM, open `/settings`, find **Streaming response** (beta tag), turn it on. It takes effect live, no restart.
3. DM a question that needs reasoning and a longer rich answer. The draft animates with reasoning inside; the final lands as a normal message.
4. Toggle off for the old behaviour; groups always get normal replies.

### What the agent does per generation

- Start the LLM call, open the draft with the Thinking block immediately.
- As tokens arrive, accumulate and update the draft at most once per second.
- At the end, sync the draft, then finalize with the full text as a normal message.
- If the stream fails mid-way, fall back to a plain-text final — never leave the user with nothing.

## Pitfalls

- Skipped finalization: the draft disappears after ~30 s with no permanent message. Always finalize.
- Partial Markdown mid-stream: unclosed `` ``` `` or `**` renders broken. Send complete blocks only, or stream raw text and apply Markdown only at finalization. The sink truncates at 4000 chars with fence closing for exactly this reason.
- Sending a draft update every token: flood / rate limit, poor animation. Batch tokens, at most one update per second.
- `RichBlockThinking` in a sent message: not stored, prohibited by the spec. Remove the Thinking block before finalization — reasoning lives in the draft only.
- A slow stream past the 30 s window: keep the draft alive with keepalive updates, or finalize early.
- Groups and channels: draft streaming is private-chat only. In a group, send a normal reply; do not attempt a draft there.
- Never build an edit loop: no edit-loop streaming anywhere, in any chat. That was never ordered and is explicitly out of scope.
- `can_stop` stays unset. Do not claim a Stop button exists.

## Verification

- With the setting on, a DM answer animates as a draft with reasoning visible inside, then lands as a normal rich message.
- With the setting off, the same DM behaves as before (no draft, one final message).
- In a group, the answer is a normal reply with no draft activity.
- A failed stream still delivers a final message (plain-text fallback), never silence.
