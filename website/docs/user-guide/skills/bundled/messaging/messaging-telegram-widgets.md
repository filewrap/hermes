---
title: "Telegram Widgets — Build and send interactive HTML widgets in Telegram"
sidebar_label: "Telegram Widgets"
description: "Build and send interactive HTML widgets in Telegram"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Telegram Widgets

Build and send interactive HTML widgets in Telegram.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/messaging/telegram-widgets` |
| Version | `1.0.0` |
| Author | serejaris, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `telegram`, `widgets`, `messaging` |
| Related skills | [`telegram-rich-messages`](../../bundled/messaging/messaging-telegram-rich-messages.md), [`telegram-client`](../../bundled/messaging/messaging-telegram-client.md), [`telegram-markdown-to-rich`](../../bundled/messaging/messaging-telegram-markdown-to-rich.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Telegram Widgets Skill

Build and send interactive HTML widgets inside a Telegram message — a live mini-app (timer, checklist, poll card, game scoreboard) that updates in place when the reader taps it, without a new message each time. This skill is a port of `tg-widgets` (serejaris/telegram-skills, Bot API 10.2) to Telethon layer 233. It wires the skill to the Hermes widget transport (`plugins/platforms/telegram/widget_messages.py`, `widget_queries.py`, `widget_transport.py`); it builds no transport itself.

## When to Use

Use when the user asks for an interactive widget, a live card, or a tap-to-update element inside a Telegram message. Use the rich-messages skill for static rich content (headings, tables, buttons) — a widget is for state that changes on tap; use the polls skill for a vote, which has its own native UI. Do not use a widget for a full mini-app: widgets are a small HTML surface inside a message, not a Web App.

## Prerequisites

- `telegram_client` is available in this Telegram conversation. The widget actions (`widget_send`, `widget_send_by_id`, `widget_upload`, `widget_answer`, `widget_edit_message`, `widget_edit_shared`, `widget_edit_inline`, `widget_get_public_data`) are owner-only with a confirmation card; all target the current chat/topic and cannot be redirected.
- Widgets render only in **beta clients** (Android 13.2.0, iOS 13.2, macOS beta 13.1.0). Stable Telegram, Telegram Desktop, and the public Bot API do not render them yet — a reader on stable sees the message without the widget. Say so before building one.
- Validation is **local only**: run `skills/messaging/telegram-widgets/scripts/widget_measure.py check widget.html` before sending. There is no API-side validation and no renderer in Hermes — the recipient's client renders.

## How to Run

1. **Write the widget HTML** by the rules below (size, theme vars, taps only). Keep it small: raw HTML ≤ 32,768 bytes, or ≤ 4,096 bytes zlib level 9.
2. **Measure it** with `widget_measure.py check widget.html`. It reports the raw size, the compressed size, and whether it fits. Fix anything it flags.
3. **Send it exactly once** with `telegram_client` `action="widget_send"` (inline HTML) or `action="widget_upload"` (upload a file first, then send by `widget_send_by_id`). **Never retry a send**: a timeout can mean the server accepted it — a retry duplicates the widget. Read the error first.
4. **Answer every tap.** The tap loop is in `widget_queries.py`: a tap arrives as a query, the handler edits the shared state first, then answers the query (empty result if unservable). An answer is not a state change — edit state before answering, always.
5. **Verify the result.** The tool returns a `message_id`. Open the message in a beta client, tap the widget, and confirm the state updates in place.

## Quick Reference

### Size and layout

| Rule | Value |
| --- | --- |
| Raw HTML | ≤ 32,768 bytes |
| Compressed (zlib level 9) | ≤ 4,096 bytes |
| Bubble width | 200–600 px; `width: 0` means auto |
| Bubble height | up to 600 px; `height: 0` means auto |
| Sizing units | `vw` allowed; `vh` never (no viewport height in a message) |
| Theme | Use the Telegram theme CSS variables; the widget inherits the client's theme |

### Input

Taps and pointer events only. No keyboard, no touch gestures beyond a tap, no scrolling inside the widget. Keep every interactive element a tap target; a tap that does nothing is a dead control — answer it anyway.

### State and requests

- `chat_data` and `public_data`: ≤ 1,024 bytes UTF-8 each.
- Request data: 1–64 bytes.
- Answer result: ≤ 1,024 bytes; the server waits up to 15 s for the answer.
- Rate: 5 requests per 5 s per user + bot.
- Shared object: at most 1 edit per second. Edit the state before answering the query.
- `shared_widget=True` and `allow_queries=True` together mean forwards may ask. There is no `fwd_allow_queries` parameter.

### Bot API → `telegram_client` action map

| Bot API method | Hermes action |
| --- | --- |
| `sendWidget` (HTML) | `widget_send` |
| `sendWidget` (by file id) | `widget_send_by_id` |
| `sendWidget` (upload) | `widget_upload` |
| `answerWidgetQuery` | `widget_answer` |
| `editMessageWidgetData` | `widget_edit_message` |
| `editSharedWidgetData` | `widget_edit_shared` |
| `editInlineWidgetData` | `widget_edit_inline` |
| `getWidgetPublicData` | `widget_get_public_data` |

### What works / what does not

| Works | Not available |
| --- | --- |
| Taps, pointer events | Keyboard input, touch gestures, scrolling |
| `vw` sizing, theme vars | `vh` sizing |
| Shared state edits, per-chat state | Wallet (not shipped) |
| Copy queries (`query_id`, `type`, `user_id`, `data`, `widget_id`) | `msg_id` / `peer` on a copy query — never read them |
| Forwards asking (shared + allow_queries) | Originals in broadcast channels — forwards only |

### Live delivery

| Chat type | Original widget | Forwarded widget |
| --- | --- | --- |
| Private DM | yes | yes |
| Group / supergroup | yes | yes |
| Channel | no (refused) | yes (forward) |

## Procedure

### Send a widget from inline HTML

```json
{
  "action": "widget_send",
  "params": {
    "html": "<!DOCTYPE html><html><body><button id=\"tap\">Tap me</button><script>/* TelegramWidget SDK */</script></body></html>",
    "chat_id": 123456789
  }
}
```

### Measure before sending

```bash
python skills/messaging/telegram-widgets/scripts/widget_measure.py check widget.html
```

The script prints the raw byte size, the zlib level 9 size, and `ok` or `too_large` with the limit that failed. It is pure zlib — no network, no Telegram call.

### Tap loop (what Hermes does for you)

1. A tap arrives as a widget query (`query_id`, `type`, `user_id`, `data`, `widget_id`).
2. The handler edits the shared widget state first (throttled to 1 edit/s).
3. The handler answers the query (empty result if the tap cannot be served).
4. The recipient's client re-renders with the new state.

## Pitfalls

- A widget that is too large fails the local measure — shrink the HTML or the embedded assets; there is no server-side fallback.
- Never retry a `widget_send`. A timeout can mean the server accepted it.
- Never answer a tap without editing the state first when the tap changes state — the answer alone does not change anything.
- Never read `msg_id` or `peer` on a copy query; they are not carried.
- Channels cannot receive originals. Send the widget to a chat and forward it, or the send is refused.
- A widget is not a rich message: `SendMediaRequest` has no `rich_message` parameter, and a widget cannot carry rich blocks. Keep the two surfaces separate.
- Stable clients do not render widgets. Test in a beta client; a stable-client screenshot that shows no widget proves nothing about the send.

## Verification

- `widget_measure.py check` reports `ok` for the HTML.
- The tool result is `ok` with a `message_id`, from exactly one send call.
- In a beta client the widget renders, a tap updates the state in place, and a second tap sees the updated state.
- `widget_get_public_data` on the sent widget returns the state the taps wrote.
