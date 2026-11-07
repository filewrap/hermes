---
title: "Telegram Markdown To Rich — Convert Markdown into a Telegram rich message"
sidebar_label: "Telegram Markdown To Rich"
description: "Convert Markdown into a Telegram rich message"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Telegram Markdown To Rich

Convert Markdown into a Telegram rich message.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/messaging/telegram-markdown-to-rich` |
| Version | `1.0.0` |
| Author | serejaris, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `telegram`, `rich-messages`, `markdown`, `messaging` |
| Related skills | [`telegram-rich-messages`](../../bundled/messaging/messaging-telegram-rich-messages.md), [`telegram-client`](../../bundled/messaging/messaging-telegram-client.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Telegram Markdown to Rich Skill

Convert a Markdown document, report, or any text content into a Telegram Rich Message for delivery with the `telegram_client` tool. This skill is a port of `tg-markdown-to-rich` (serejaris/telegram-skills, Bot API 10.2) to Telethon layer 233. It converts only; it sends nothing itself.

## When to Use

Use when the user asks to send Markdown to Telegram, convert a doc to a rich message, or publish a report to a bot. Use the rich-messages skill for composing rich messages directly as HTML or literal blocks, and the polls skill when the post is a poll — a poll cannot go inside a rich message.

## Prerequisites

- `telegram_client` is available in this Telegram conversation. `rich_message_send` and `quote_reply` are the only actions an admitted human may call directly; both always target the active profile/chat/topic and cannot be redirected.
- Media bindings need a resolved source first: a `file_id`, an HTTP(S) URL, or a local path. The resolver is `plugins/platforms/telegram/rich_media.py` (`resolve_rich_media`, `bind_rich_media`); the agent calls it through the tool, never by hand.

## How to Run

1. **Pick a route.** The string route keeps the Markdown as the message: send the Markdown text with `format: "markdown"` and let the server parse it. The blocks route converts it to native blocks: send with `format: "blocks"`.
2. **String route (default).** Call `telegram_client` with `action="rich_message_send"`, `params.content` set to the Markdown text, and `params.format` set to `"markdown"`. Media references in the text use `tg://photo?id=<id>`, `tg://video?id=<id>`, or `tg://audio?id=<id>`; each id must have a binding (see below).
3. **Blocks route.** Call `rich_message_send` with `params.format` set to `"blocks"` and the same Markdown text. The adapter converts it to `PageBlock*` objects: headings, dividers, preformatted code with a copy button, blockquotes, tables, lists (including task checkboxes), and math. Partial Markdown mid-stream is a blocks-route concern only when streaming — see the rich-streaming skill.
4. **Bind media before sending.** For the string route, resolve each `tg://…?id=` reference to a server Photo/Document first (`file_id` via `resolve_bot_file_id`, URL via external media + upload, local path via upload). A reference without a binding and a binding without a reference are both rejected before any send. For the blocks route, media needs an already-resolved numeric server id (`tg://photo?id=123`); string ids cannot be resolved on that route — use the literal-blocks builder (`blocks_from_dict` in `plugins/platforms/telegram/rich_dict_blocks.py`) with a media map instead.
5. **Verify the result.** The tool returns `message_id`. Open the message and confirm the layout, then report the ID plainly.

## Quick Reference

### Markdown → rich mapping

| Markdown syntax | Rich block / inline |
| --- | --- |
| `# H1` … `###### H6` | `heading` block (size 1–6) |
| Paragraph text | `paragraph` block |
| `**bold**` / `__bold__` | `bold` inline |
| `*italic*` / `_italic_` | `italic` inline (single-star `*…*` is handled by the pre-pass; Telethon reads `*` as bold) |
| `` `code` `` | `code` inline |
| `~~strikethrough~~` | `strikethrough` inline |
| `==marked==` | `marked` inline |
| `\|\|spoiler\|\|` | `spoiler` inline |
| `` ```lang … ``` `` | `pre` block (with language; short blocks get a copy button) |
| `[text](https://github.com/NousResearch/hermes-agent/blob/main/skills/messaging/telegram-markdown-to-rich/url)` | `url` inline |
| `[text](mailto:…)` | `email` inline |
| `[text](https://github.com/NousResearch/hermes-agent/blob/main/skills/messaging/telegram-markdown-to-rich/tel:…)` | `phone` inline |
| `[text](tg://user?id=…)` | `text_mention` inline |
| `$LaTeX$` | `mathematical_expression` inline |
| `$$LaTeX$$` / ` ```math` | `mathematical_expression` block |
| `---` | `divider` block |
| `> text` | `blockquote` block (nested `>>` flattens to text — no native nesting) |
| `- item` / `* item` | `list` block (unordered) |
| `1. item` | `list` block (ordered) |
| `- [ ] task` / `- [x] task` | `list` block with checkbox |
| GFM table `\| … \|` | `table` block |
| `![alt](tg://photo?id=123)` (block-level) | `photo` block, numeric server id only |
| `[^id]: …` footnote | superscript numeral + trailing definitions paragraph (no native footnote type in layer 233) |
| `<u>` / `<ins>` | `underline` inline |
| `<sub>` / `<sup>` | `subscript` / `superscript` inline |
| `<aside>…</aside>` | `pullquote` block |
| `<details [open]><summary>…</summary>…</details>` | `details` block |
| `<tg-collage>…</tg-collage>` | `collage` block |
| `<tg-slideshow>…</tg-slideshow>` | `slideshow` block |
| `<tg-map lat="…" long="…" zoom="…" width="…" height="…"/>` | `map` block (zoom 0–24, default 15) |

### Media binding fields

| Binding field | Meaning |
| --- | --- |
| `id` | 1–64 characters, `[A-Za-z0-9_-]+`; referenced as `tg://<scheme>?id=<id>`. |
| `media.type` | `photo`, `video`, `audio`, `document`, `animation`, `voice_note`. |
| `media.media` | `file_id`, HTTP(S) URL, or local path. `attach://` + multipart is Bot API-only and is not ported. |
| `duration`, `performer`, `title` | Audio player metadata. There is no field for this on `InputRichFile*` — the metadata is recorded on the binding but does not travel on the rich file; the audio still plays. |
| `has_spoiler` | Photo/video spoiler flag. |

### Limits enforced before sending

| Limit | Value |
| --- | --- |
| Max characters (UTF-8) | 32,768 |
| Max blocks (incl. nested) | 500 |
| Max nesting depth | 16 |
| Max media blocks | 50 |
| Max table columns | 20 |

## Procedure

### Send a Markdown report, string route

```json
{
  "action": "rich_message_send",
  "params": {
    "content": "## Weekly Digest\n\nLead paragraph.\n\n### Top Topics\n\n- Item one\n- Item two\n",
    "format": "markdown"
  }
}
```

### Send the same report as native blocks

```json
{
  "action": "rich_message_send",
  "params": {
    "content": "## Weekly Digest\n\nLead paragraph.\n\n### Top Topics\n\n- Item one\n- Item two\n",
    "format": "blocks"
  }
}
```

### Send Markdown with a bound photo

Resolve the photo first (the tool's media binding), then reference it in the text:

```json
{
  "action": "rich_message_send",
  "params": {
    "content": "# Title\n\n![](tg://photo?id=cover)",
    "format": "markdown"
  }
}
```

The binding for `cover` must exist (a `file_id`, a URL, or an uploaded local file) or the send is rejected before anything goes out.

## Pitfalls

- `attach://name` sources are Bot API-only and are not ported. A local file is uploaded through the resolver instead; a binding that names an `attach://` source is rejected.
- A reference without a binding and a binding without a reference are both rejected. Check both directions before sending.
- Footnotes have no native type in layer 233 (`TextReference` does not exist). Refs become superscript numerals and the definitions land in a trailing paragraph — content is preserved, the footnote link is not.
- Nested blocks inside table cells are not supported; cell content is treated as inline text.
- `RichBlockThinking` is not producible from Markdown — it is valid only in a draft, never in a sent message. See the rich-streaming skill.
- HTML tags outside the rich HTML spec are passed through; Telegram ignores unknown tags.
- A timeout can mean the server accepted the request. Do not resend a different payload shape; read the error first.

## Verification

- The tool result is `ok` with a `message_id`.
- Open the message: headings render as headings, tables keep their columns, the foldout opens, the code block carries its copy button, and bound media shows.
- On a validation failure (over a limit, unbound media, bad binding) the tool fails before sending — nothing is delivered and nothing needs undoing.
