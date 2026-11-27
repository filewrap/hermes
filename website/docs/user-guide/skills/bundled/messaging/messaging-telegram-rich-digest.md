---
title: "Telegram Rich Digest — Publish digests as one structured Telegram rich message"
sidebar_label: "Telegram Rich Digest"
description: "Publish digests as one structured Telegram rich message"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Telegram Rich Digest

Publish digests as one structured Telegram rich message.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/messaging/telegram-rich-digest` |
| Version | `1.0.0` |
| Author | serejaris, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `telegram`, `rich-messages`, `digest`, `messaging` |
| Related skills | [`telegram-rich-messages`](../../bundled/messaging/messaging-telegram-rich-messages.md), [`telegram-markdown-to-rich`](../../bundled/messaging/messaging-telegram-markdown-to-rich.md), [`telegram-client`](../../bundled/messaging/messaging-telegram-client.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Telegram Rich Digest Skill

Send a daily or weekly digest, community summary, status report, newsletter, or channel article as one structured Telegram Rich Message instead of a wall of plain text. This skill is a port of `tg-rich-digest` (serejaris/telegram-skills, Bot API 10.2) to Telethon layer 233. It covers the digest/report pattern specifically; for the complete block reference see the rich-messages skill, and for converting an existing Markdown report see the markdown-to-rich skill.

## When to Use

Use when the user asks to send a weekly digest, post a summary to a channel, publish a newsletter or a status report, or format a report as a rich message. Do not use it for a plain announcement — the normal reply path already formats those — or for a poll, which cannot go inside a rich message.

## Prerequisites

- `telegram_client` is available in this Telegram conversation. `rich_message_send` and `quote_reply` are the only actions an admitted human may call directly; both always target the active profile/chat/topic and cannot be redirected.
- Media bindings need a resolved source first: a `file_id`, an HTTP(S) URL, or a local path (see the markdown-to-rich skill). `attach://` + multipart is Bot API-only and is not ported.
- The preflight gate lives in `plugins/platforms/telegram/digest_preflight.py` (`preflight_digest`). Run it before every edition; it is local except for the link-resolution check, which only runs when a resolver is supplied.

## How to Run

1. **Pick a layout.** Flat for a short bullet roundup (under ~1500 chars): everything visible, `<details>` reserved for the long tail. Preview + full version for an article: headline + lead + one cover image, then the entire article inside one `<details>` block.
2. **Build the body** as Rich HTML or Markdown (see Anatomy below), with media bound through `tg://…?id=` references.
3. **Run the preflight gate** on the body before sending. Fix every blocking failure; fix or accept every warning. Do not send with a blocking failure open.
4. **Send once** with `telegram_client` `action="rich_message_send"` (`format: "html"` or `"markdown"`). One message per edition — a follow-up post with forgotten screenshots splits the artifact and cannot be edited into the original.
5. **Verify the result.** The tool returns a `message_id`. Open the message and confirm the layout, then report the ID plainly.

## Quick Reference

### Anatomy of a digest

```
[heading h2]   — digest title + date range
[paragraph]    — 1–3 sentence lead: what happened, key number
[heading h3]   — topic 1
[list]         — 3–7 bullet items for topic 1
[heading h3]   — topic 2
[list]         — ...
[details]      — "All links / Full list" — long-tail, closed by default
[divider]      — visual separator before footer
[collage]      — photo report (optional)
[map]          — event location (optional)
[footer]       — date, source, issue number
```

### Preview + full version layout

```
[heading h1]   — the claim, as a headline
[paragraph]    — 2–3 short paragraphs: the fact, your own check, the consequence
[photo]        — cover image (one, no more)
[details]      — "Read the full version" — the ENTIRE article, closed by default
```

Budget the preview: roughly **500 visible characters total** before `<details>`, no single paragraph over ~220 — past that the feed truncates the preview itself. Media before `<details>` counts as part of the preview. Keep the sign-off inside `<details>`; anything after the closing tag reads as a detached fragment in the feed.

### Length budgeting

| Limit | Value |
| --- | --- |
| Max UTF-8 characters (incl. alt-text, formulas) | **32,768** |
| Max blocks (all nested: list items, table rows, details content, quotation blocks) | **500** |
| Max nesting levels | **16** |
| Max media attachments (photos + videos + audio) | **50** |
| Max table columns | **20** |

Count blocks before sending: every list item counts, every `blocks` entry inside `details` counts, a collage with 8 photos is 1 + 8 = 9 blocks.

### Overflow strategy

When a digest is near the limits, cut in this order: trim `details` content first, reduce list depth second, split into two messages third (end message 1 with a divider + `"Continued →"` footer; open message 2 with a `"(continued)"` heading), drop media last.

## Procedure

### Flat digest

```html
<h2>Weekly Digest &mdash; Jun 9&ndash;15</h2>
<p>This week the community hit <b>1,200 members</b>. Three tools shipped, one hot thread ran 80+ replies.</p>
<h3>Top Discussions</h3>
<ul>
  <li><a href="https://example.com/thread/42">LLM context limits &mdash; practical patterns</a> &mdash; 81 replies</li>
  <li><a href="https://example.com/thread/55">Prompt caching deep dive</a> &mdash; 80% cost reduction reported</li>
</ul>
<details><summary>All links this week (9)</summary>
<ol>
  <li><a href="https://example.com/thread/42">LLM context limits &mdash; practical patterns</a></li>
</ol>
</details>
<hr/>
<footer>AI Builders Community &mdash; Issue #24 &mdash; Week of June 9&ndash;15, 2026</footer>
```

### Preview + full version

```html
<h1>Voice model ships with Russian audio-to-audio</h1>
<p>The vendor shipped <code>voice-think-fast-2.0</code> on July 29. First audio in 0.7 s, $0.08 per minute.</p>
<p>We tested Russian audio-to-audio: the model heard the question and answered <b>323</b>.</p>
<img src="tg://photo?id=cover"/>
<details><summary>Read the full version</summary>
  <h2>What shipped</h2>
  ...
</details>
```

### Media as evidence, not decoration

- One source screenshot per factual claim a reader might otherwise have to take on trust — a release page, a pricing table, a dashboard.
- Audio and video belong in the body, next to the claim, not collected into a trailing block.
- Give every audio binding its metadata (`duration`, `performer`, `title`).
- A collage at the end is a photo report; a screenshot under the claim it supports is evidence.

### Map embed

```html
<tg-map lat="52.5200" long="13.4050" zoom="15"/>
```

Zoom is 0–24 (13–20 is the practical range for an event-location tile). One static location tile, no interactive markers.

## Pitfalls

- A digest published on a schedule needs the preflight gate — every check in it has a failure mode that only shows up after the post is live and uneditable. Never skip it.
- Media before `<details>` counts as part of the preview: one cover image, no more.
- A trailing fragment after the closing `</details>` reads as detached in the feed. Keep the sign-off inside.
- A bare `@handle` in text links as a Telegram user. External handles must be full URLs.
- A dead link in a published digest cannot be fixed by editing. The link check blocks the send.
- Rich rendering on older clients is not documented. Test in a private chat or staging channel first, in both mobile and desktop clients.
- `attach://` names are Bot API-only. A digest that references one fails the preflight; rebind the media through `file_id`, URL, or upload instead.

## Verification

- The preflight gate reports no blocking failures. Warnings are fixed or explicitly accepted.
- The tool result is `ok` with a `message_id`.
- Open the message: the preview reads as a clean card, the `<details>` opens to the full version, media sits next to the claim it supports, and the footer carries date, source, and issue number.
- The same edition was not published before (the duplicate check compares against the sent index).
