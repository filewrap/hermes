---
sidebar_position: 1
title: "Telegram"
description: "Connect Hermes to Telegram through Telethon and MTProto"
---

# Telegram

Hermes connects Telegram bots through **Telethon and MTProto**. The gateway, live media sends, and standalone/cron delivery all use this native client. There is no Bot API polling, `getUpdates`, webhook, or HTTP-send fallback.

## Requirements

Configure all three credentials:

- **Telegram API ID** and **API hash** from [my.telegram.org/apps](https://my.telegram.org/apps).
- **Bot token** from [@BotFather](https://t.me/BotFather).

A bot token alone is not enough for MTProto. Hermes stores the API hash and bot token as secrets; keep them out of `config.yaml` and source control.

## Setup

Run `hermes gateway setup` and choose Telegram. Enter the API ID, API hash, and bot token when prompted. The environment-variable setup is:

```dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=123456:your_bot_token
```

Optional access and delivery settings:

```dotenv
TELEGRAM_ALLOWED_USERS=123456789,987654321
TELEGRAM_ALLOW_ALL_USERS=false
TELEGRAM_GUEST_MODE=true
TELEGRAM_HOME_CHANNEL=-1001234567890
TELEGRAM_CRON_THREAD_ID=12345
TELEGRAM_PROXY=socks5://127.0.0.1:1080
```

`TELEGRAM_PROXY` also accepts `socks4://` and `http://` (HTTP CONNECT). If unset, Hermes uses the normal direct connection or the configured system proxy. Bot API fallback-IP and HTTP-pool settings are not used.

The same three access/display knobs live in `config.yaml` under `telegram:` (the platform `extra:` block carries them to the adapter):

```yaml
telegram:
  allowed_chats: ""   # if set, ONLY respond in these group/supergroup chat IDs
  channel_prompts: {} # per-chat/topic ephemeral system prompts (topics inherit from parent group)
  reactions: false    # add 👀/✅/❌ reactions to messages during processing
```

- `telegram.allowed_chats` — group/supergroup chat IDs the bot responds in, comma-separated (or a JSON list); also settable as `TELEGRAM_ALLOWED_CHATS`. When set, every group chat not on the list is ignored.
- `telegram.channel_prompts` — per-chat (or per-forum-topic) ephemeral system prompts, keyed by chat ID; topics inherit the parent group's prompt.
- `telegram.reactions` — when `true`, the bot reacts to messages with 👀 while working, ✅ on success, and ❌ on failure.

### Per-profile sessions

Telethon keeps a persistent bot session in the active Hermes profile, normally at `sessions/telegram/bot.session` under that profile's home. A populated legacy `telegram_sessions/` directory is preserved. Each profile has its own session: do not copy or share `.session` files between profiles or machines, and treat them as credentials.

## What works

- Incoming and outgoing Telegram text, slash commands, photos, videos, audio/voice files, documents, and stickers.
- Private chats, groups, and forum-topic reply routing.
- Group mention gating, the configured user/chat allowlists, and replies to the bot's own messages as explicit triggers.
- Message edits/deletes, typing indicators, emoji reactions, link-preview control, and standalone/cron delivery.
- Native MTProto Rich Messages for text, colored callback buttons, paginated `/help`, and interactive GuestBot settings.
- Replies can use Markdown or Telegram Rich HTML. Hermes selects Rich HTML when supported HTML tags appear outside code; otherwise it uses Rich Markdown. Telegram Rich HTML supports structured elements such as tables, headings, checklists, and collapsible details, but not arbitrary CSS, classes, or `<div>` layout. Markdown pipe tables are sent as readable labeled rows.
- Telegram Guest Mode updates routed through Hermes' normal AI message handler; GuestBot is enabled by default and remains subject to the configured user/chat authorization checks.
- Outbound messages split at Telegram's 4096 UTF-16-unit limit; media captions are limited to 1024 UTF-16 units.

Use the usual Hermes slash commands by sending them as messages. Configure access through `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_GROUP_ALLOWED_USERS`, `TELEGRAM_GROUP_ALLOWED_CHATS`, and the corresponding platform allowlist settings.

### GuestBot

Guest Mode lets Telegram deliver a user's message plus any reference excerpts Telegram provides to the bot. Hermes processes these updates with its normal AI message handler and sends the answer back using Telethon's native Guest Mode result method. Guest conversations use a separate per-user thread rather than the ordinary group transcript; only the request and Telegram-supplied excerpts are available. Existing Hermes authorization and allowlist checks remain in force. Telegram Guest Mode must also be enabled for the bot in BotFather.

Open `/help` and choose **GuestBot settings**, or send `/guestbot`, `/guestbot on`, or `/guestbot off`. The interactive toggle is saved in the active profile's `platforms.telegram.guest_mode` setting. GuestBot is enabled by default; set `guest_mode: false` to disable it in `config.yaml`. This setting controls Hermes' local processing only and does not change BotFather configuration.

### Your private skills

Open `/skills`, or choose **Your skills · Add / manage** in `/settings`. The menu lets you add a skill, browse your private library, read or export a package, edit its instructions, remove it with confirmation, and choose skills to auto-load. Profile skills remain read-only. Your uploaded files and auto-load choices belong only to your Telegram account within the active profile; they are not shared with other users or group chats.

Choose **Send / forward ZIP or .md**, **GitHub repository / skill link**, or **Write / paste SKILL.md here**. A direct skill document, attachment, or public GitHub link is also detected in a private chat and offered for review. Detection never installs anything automatically: inspect the name, description, included files, security findings, and complete document, then confirm adding or replacing the skill. Private repository credentials are not accepted in chat. Uploaded packages are bounded and checked for unsafe paths, links, and credential material; no scripts run during import. Scanning cannot guarantee that instructions are safe, so use trusted sources.

Direct writing means sending a **complete skill document**, not a title or an idea. Send a UTF-8 `SKILL.md` file or paste the same structure:

```markdown
---
name: daily-notes
description: Use when summarizing the user's daily notes.
---
# Daily notes

## Instructions
Summarize the supplied notes, separate facts from assumptions, and ask before sharing them.
```

Use a lowercase hyphenated name, a description explaining when to use the skill, and non-empty Markdown instructions. A ZIP can include companion scripts, references, and assets alongside `SKILL.md`. Editing instructions retains existing companion files. Send `/cancel` to abandon an input request; it expires after ten minutes. Importing a skill does not automatically select it. Choose **Auto-load skills**, then `/new` for a new session; skill changes do not rebuild the active conversation's cached prompt. A skill can also be explicitly invoked with its slash command in a new session.

## Not ported from the Bot API adapter

These Bot API features and settings are not used by the MTProto transport, and Hermes will not silently fall back to the Bot API:

- `TELEGRAM_WEBHOOK_URL`, webhook ports/secrets, Bot API long polling, `getUpdates`, and self-hosted/local Bot API servers.
- Bot API HTTP timeout/pool options, direct-IP fallback lists, Bot API file IDs, and `api.telegram.org` REST sends.
- Telegram UI command-menu registration, arbitrary inline-query result pickers, Bot API callback protocols, and Bot API-specific approval/confirmation controls. Native callback buttons are used only for Hermes' built-in `/help` navigation and GuestBot settings.
- Bot API draft rendering and global bot-profile status/description updates. Rich text and the built-in callback buttons are implemented with Telethon's MTProto constructors, not Bot API requests.
- Bot API-specific advanced mention-pattern routing, passive observation of unmentioned group messages, and topic-management actions. Basic explicit bot mentions, allowlists, and reply routing remain available.

If an old configuration contains any of those keys, remove them; they do not configure the Telethon client.

## Migration from the Bot API transport

1. Obtain an API ID and API hash from [my.telegram.org/apps](https://my.telegram.org/apps); keep the existing bot token.
2. Run `hermes gateway setup` to save all three credentials for the correct profile.
3. Remove old webhook, HTTP-timeout, fallback-IP, or local Bot API server settings.
4. Stop the old gateway process and start Hermes again. The new client creates or resumes that profile's MTProto session.

## Troubleshooting

- **Missing credentials:** verify that the active profile has a positive `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_BOT_TOKEN`.
- **Proxy connection errors:** check the proxy URL scheme and credentials. Supported schemes are `socks5`, `socks4`, and `http`.
- **Messages are ignored in a group:** verify the sender/chat allowlist and mention requirement; with mention gating enabled, mention the bot or reply to one of its messages.
- **Session file access:** stop the gateway before moving a profile. Do not delete or copy the session while Hermes is running.

For the protocol distinction and bot sign-in requirements, see Telethon's [Bot API vs. MTProto guide](https://docs.telethon.dev/en/stable/concepts/botapi-vs-mtproto.html) and [sign-in documentation](https://docs.telethon.dev/en/stable/basic/signing-in.html).
