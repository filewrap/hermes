# Hermes changelog

Public stable snapshots of the Hermes fork. Each entry covers one
snapshot: a fresh single commit whose tree is a fork-main tree, with
`Dockerfile.hf` self-pinned to the previous snapshot's SHA.

## Snapshot 2026-11-07 — fork main `799f565e5c` (v0.22.7 + 10)

81 commits since the 2026-11-04 snapshot (`392f3e5e31`).
Previous snapshot: `a930bc38dd`.

### Upstream sync — v0.22.7

- Merged upstream stable v0.22.7 and the v0.22.7 canary; sync base
  recorded in `hermes_cli/version_info.py`. Fork version is
  `0.22.7-10:799f565e5c`.

### Access control redesign

- Removed `/allowall` and all allow-all machinery. Only the owner or
  the universal allowlist gets replies, everywhere; non-alloweds are
  silently ignored.
- `/trigger "word"` (single word, group-only, `"off"` disables) and
  `/allow` / `/disallow` by reply, @username, or numeric id.
- Startup warns (never refuses) when the owner is missing or invalid.

### Telegram rich messages

- 54 command responses reformatted as structured rich cards, plus
  rich `/kanban`, `/sessions`, `/insights`, `/plugins`, `/providers`,
  `/managedbots`, and `/b2b` menus; `hm:run:/` navigation buttons
  removed from every rich card.
- `/busy` is a structured status card (queue / steer / interrupt).
- `/version` shows the full Version(s) section (Hermes Agent, Canary,
  Fork version, build SHA/branch/message/date).
- Settings badges sit right after the setting name, blue, tappable
  (toast: "new function — enjoy").
- `/memories` and `/memory all` list every saved memory entry as
  collapsibles, 10 per page, no tables.

### Thread routing (root fix)

- `thread_id` is derived from `reply_to_top_id` only for a real
  forum topic; plain-group reply chains no longer split the session.
  Session keys now name the chat type (`:dm:`, `:group:`, `:forum:`,
  `:channel:`).

### Streaming response (Beta)

- New owner-only "Streaming response" setting (Personal settings).
  In a private 1-1 DM the answer streams as a live rich draft:
  reasoning shows first as a Thinking block and stays visible above
  the content. Groups get a normal reply, never an edit loop.
- The live draft shows a native Stop button; pressing it halts the
  generation and delivers the streamed partial as a real message.
- Guest queries get a "Thinking…" status message, edited in place
  with the answer. Guestbot typing actions (draft previews) were
  removed — they never worked live.

### Telethon layer 233

- Telethon upgraded to 1.45.0 / layer 233 (codeberg v1 `d8a33dad`).
- HTML widgets in messages (layer 233 `messageMediaWidget`): send,
  edit, tap queries and answers, shared-widget state — bot-only,
  local validation only.
- Ported the 7 telegram-skills (rich messages, markdown-to-rich,
  streaming, polls, digest, reply-rewrite, widgets) to Telethon
  layer 233 natively, including a literal-dict → InputRichMessage
  blocks route and rich-media resolution.

### Multi-account control

- Removed the upstream second-client ban. Hermes never does
  direct/script operations on its own session; for that use the
  dedicated `telegram_client` tool. New `telegram-sessions` skill
  covers session lifecycle, login, states, password/2FA, and 10+
  accounts in different libraries.

### Other

- Providers: custom-provider list, sharing, owner-only delete;
  dangling-route rescue prefers the last used provider+model, with
  a `fallback.auto_fallback` toggle (default on).
- `telegram_client` gains a structured read-only `request` action
  (5 Get* methods, owner-only).
- nemo-relay 0.11.0; Voice Messages STT/TTS docs; provider-sharing
  enforcement ported into the background-task mixin.
- pyasn1 aligned to 0.6.5, unblocking `uv lock`.
- `/reasoning` card renders its Usage label in italic (was literal
  underscores); copy pills kept.
