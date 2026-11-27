---
name: telegram-visualize
description: Show precise widgets in DMs and rich messages in groups.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [telegram, widgets, rich-messages, messaging]
    category: messaging
    related_skills: [telegram-widgets, telegram-rich-messages, telegram-client]
---

# Telegram Visualize Skill

Show a precise visual with one `telegram_client` call: a pre-made widget in a DM, or the same content as a precise native rich message in any other chat. Templates cover a metric card, a progress tracker, a checklist, a mermaid flowchart, a canvas chart, and a status board. This skill does not pin widgets to a dashboard (that needs boards, which Hermes does not have yet) and does not build free-form HTML widgets — use the telegram-widgets skill for those.

## When to Use

Use when the user asks to show a number, a progress state, a checklist, a flow, a chart, or a status board in the current Telegram chat. Use the telegram-widgets skill when the visual needs hand-written HTML or tap-to-update state. Use the telegram-rich-messages skill when the content is a static rich card with no widget form. Do not use a widget for a full mini-app: widgets are a small HTML surface inside a message, not a Web App.

## Prerequisites

- `telegram_client` is available in this Telegram conversation. `widget_show` always targets the current chat/topic and cannot be redirected.
- Widgets go to DMs only. `widget_show` sends the widget when the current chat is a DM; in a group, supergroup, or channel it sends the same content as one rich message instead. `widget_send` and `widget_send_by_id` refuse any chat that is not a DM outright.
- Widgets render only in **beta clients** (Android 13.2.0, iOS 13.2, macOS beta 13.1.0). Stable Telegram, Telegram Desktop, and the public Bot API do not render them yet — a reader on stable sees the message without the widget. Say so before showing one. The rich surface renders everywhere.
- Validation is **local only**: the kit refuses a template whose widget HTML breaks the size limits, with the limit that failed. There is no API-side validation and no renderer in Hermes — the recipient's client renders the widget, Telegram renders the rich message.

## How to Run

1. **Pick a template** from the table below and shape the data to it. Keep labels short: every string the template takes is capped, and the widget HTML must fit the size limits.
2. **Call `telegram_client` with `action="widget_show"` once**, with `template`, `title`, and `data`. Add `companion` (Rich HTML) and `buttons` only when the delivery needs them. **Never retry a show**: a timeout can mean the server accepted it — a retry duplicates the delivery. Read the error first.
3. **Read the result.** It names the `surface` (`widget` in a DM, `rich` elsewhere), the `template`, and the `message_id` (plus `widget_id` and `companion_message_id` on the widget surface). Report the surface plainly: a group never gets a widget, by design.
4. **Verify the result.** In a DM, open the message in a beta client and confirm the widget renders. In a group, confirm the rich message carries the same values, tables, and buttons.

## Quick Reference

### Templates

| Template | `data` shape | Widget (DM) | Rich (group) |
| --- | --- | --- | --- |
| `metric` | `{label, value, delta?, note?}` | Big value, label, delta chip | Heading, bold value, note |
| `progress` | `{label, done, total, steps? [{label, done}]}` | Bar + fraction + step list | Heading, text bar in `<pre>`, steps table |
| `checklist` | `{label, items [{label, done}]}` | ✓/○ list with a done count | Table with ✓/○ and a done count |
| `flow` | `{mermaid}` | Inline SVG flowchart | Edge table + `<details>` with the mermaid source |
| `chart` | `{label, kind bar\|line, labels, values, unit?}` | `<canvas>` chart + value list fallback | Label/value table + text bars in `<pre>` |
| `status` | `{label, rows [{name, state, detail?}]}` | Name/state/detail table | Same table |

Bounds: title 1–128 characters; labels 1–128; notes 1–512; lists and rows 1–24 items; chart labels and values 2–24, same length; progress `0 <= done <= total`, total 1–1000; mermaid source 1–4,000 characters.

### Mermaid subset (flow only)

`flowchart TD` or `flowchart LR` first (`graph`, `TB`, `BT`, `RL` spellings map onto the same two directions). Node shapes: `A[Label]`, `A[[Label]]`, `A(Label)`, `A([Label])`, `A((Label))`, `A{Label}`, `A[/Label/]`. Edges: `A --> B`, `A --- B`, `A -.-> B`, `A ==> B`, with optional `-- label -->` text, and chains (`A --> B --> C`). `%%` comments and `;` separators work. Subgraphs, class lines, `&` joins, and other diagram kinds are refused with the line that failed — shrink or restate the flow instead of sending a wrong diagram.

### Buttons and companion

`buttons` is the same up-to-8 native one-way shape as `rich_message_send`: `{type: url|copy|profile, label, value, style?}`. In a DM the buttons travel on a rich companion message sent just before the widget (the classic widget keyboard is callback-only, so one-way buttons cannot sit under the widget); `companion` is that message's Rich HTML, or a bare title heading when only buttons are given. On the rich surface the buttons append to the template's rich message as one trailing row. There is no callback button from this action.

### Size and layout

| Rule | Value |
| --- | --- |
| Widget raw HTML | ≤ 32,768 bytes |
| Widget compressed (zlib level 9) | ≤ 4,096 bytes |
| Rich message | ≤ 32,768 characters |
| Bubble width | 200–600 px; `width: 0` means auto |
| Bubble height | up to 600 px; `height: 0` means auto |
| Theme | Telegram theme CSS variables; the widget inherits the client's theme |

## Procedure

### Show a metric

```json
{
  "action": "widget_show",
  "params": {
    "template": "metric",
    "title": "Deploy health",
    "data": {"label": "Success rate", "value": "99.2%", "delta": "+0.4% this week", "note": "Across 41 deploys."}
  }
}
```

### Show a mermaid flow with buttons

```json
{
  "action": "widget_show",
  "params": {
    "template": "flow",
    "title": "Release pipeline",
    "data": {"mermaid": "flowchart LR\n  A[Build] --> B[Test]\n  B -- pass --> C[Deploy]\n  B -- fail --> D[Fix]\n  D --> A"},
    "buttons": [{"type": "url", "label": "Open pipeline", "value": "https://example.com/pipeline", "style": "primary"}]
  }
}
```

In a DM this sends the flow as an inline SVG widget (plus a companion message carrying the button); in a group it sends the edge table, the mermaid source in a foldout, and the button row — the same content, no widget.

### Show a canvas chart

```json
{
  "action": "widget_show",
  "params": {
    "template": "chart",
    "title": "Signups this week",
    "data": {"label": "New accounts", "kind": "bar", "labels": ["Mon", "Tue", "Wed", "Thu", "Fri"], "values": [12, 19, 9, 24, 17]}
  }
}
```

The widget draws the chart on a `<canvas>` with compact inline JavaScript and lists the values underneath as fallback text. The rich surface renders the same values as a table plus text bars.

## Pitfalls

- A template that breaks the widget size limit is refused before anything is sent — shrink the flow, drop rows, or shorten labels; there is no server-side fallback.
- Never retry a `widget_show`. A timeout can mean the server accepted it.
- A tap on the widget itself gets the empty answer in this version: the templates are display surfaces with no tap handler registered. Put actions on the companion buttons instead of inside the widget.
- Callback buttons are not available from this action. Only one-way buttons (URL, copy, profile) may be sent.
- The companion message exists only on the DM surface. In a group the template's rich message carries the buttons; a `companion` is ignored there.
- A flow that uses subgraphs, class lines, or `&` joins is refused at validation. Restate it in the supported subset.
- Stable clients do not render widgets. Test in a beta client; a stable-client screenshot that shows no widget proves nothing about the send.
- Widgets cannot load external scripts from a CDN — the kit renders flows to SVG and charts to inline JavaScript, so no delivery depends on a script fetch. Whether a Telegram widget can fetch a CDN script at all is unverified live; do not build a widget that needs one.
- A widget is not a rich message: `SendMediaRequest` has no `rich_message` parameter, and a widget cannot carry rich blocks. Keep the two surfaces separate — the kit builds each from the same data.

## Verification

- The tool result is `ok` with `surface` naming the delivery, `template` naming the template, and a `message_id`. A `pending_confirmation` result is not a send; `widget_show` never returns one.
- In a DM with a beta client the widget renders, the title and values match the data, and the companion message (when given) carries the buttons.
- In a group the rich message carries the same values: the metric value, the checklist count, every flow edge in the table, the chart values in the table and text bars.
- On a refused show, the error names the limit or the bad field. Fix the data and send once.
