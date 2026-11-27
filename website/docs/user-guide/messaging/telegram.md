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

Run `hermes gateway setup` and choose Telegram. Enter the API ID, API hash, and bot token when prompted. If you don't have a bot yet, create one in [@BotFather](https://t.me/BotFather): send `/newbot`, choose a display name and a username ending in `bot`, and BotFather replies with the API token. Keep the token secret; if it leaks, revoke it with `/revoke` in BotFather.

The environment-variable setup is:

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

Use a numeric ID for a DM home channel: an `@username` works only for a public channel or supergroup — Telegram answers "Chat not found" for a person's handle.

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

### Business Mode (chat automation)

When the bot is connected as a Telegram Business chatbot, customer messages
get direct replies — the reply goes straight to the customer via the
business connection, with no approval step. The owner sees what was sent
in Recent and as a minimal sent notice. The `telegram.business` block
tunes it:

```yaml
telegram:
  business:
    enabled: true             # master kill switch: false records connections but never replies
    trigger_mode: direct      # direct (every customer message replies) or trigger (only on the trigger word)
    trigger_word: ""          # single word; empty = off
    debounce_seconds: 8       # coalesce a typing burst into one reply
    max_customer_text_chars: 4000
    owner_persona: "You are replying on behalf of the account owner. Match the tone of a personal message — warm, direct, and concise. Do not introduce yourself as an assistant or AI. Reply in the same language the customer used."
```

- `telegram.business.enabled` — when `false`, connections are still recorded but no replies are sent.
- `telegram.business.trigger_mode` / `telegram.business.trigger_word` — `direct` replies to every customer message; `trigger` replies only to messages containing the trigger word (or replies to a message the bot sent).
- `telegram.business.owner_persona` — the replying instruction for the reply persona.

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

### Staying silent in a group

A bot that reads every group message can answer exactly `[SILENT]` (or another [silence token](index.md#intentional-silence-tokens)) when a message doesn't need it. Whether that silence stands depends on who the message addresses:

| Group message | Bare silence token |
|---|---|
| Mentions the bot, replies to it, or is a `/command` | Replaced by a short notice, so a question to the bot never vanishes |
| Replies to another member, or opens by @mentioning someone else | Stays silent |
| Any other message without a `?` | Stays silent |
| Any other message with a `?` (it may be a question for the bot) | Replaced by the notice |

A message still gets the notice while the bot's own identity is unknown, or when it replies to a message whose author is unknown or that was posted as a chat (a linked channel's post, an anonymous admin). A voice note, video note or audio file without a caption also gets the notice, because its words are not in the message text. A forum topic's root message is not a reply to another member.

### Group routing

Use `telegram.allowed_topics` (env `TELEGRAM_ALLOWED_TOPICS`) as a forum-topic whitelist — when set, Hermes ignores messages from any other group/forum topic. DMs are never filtered. Telegram's General topic is normalized to topic ID `1`.

Use `telegram.free_response_chats` / `telegram.free_response_topics` (env `TELEGRAM_FREE_RESPONSE_CHATS` / `TELEGRAM_FREE_RESPONSE_TOPICS`) to bypass `require_mention` for a whole chat or a single forum topic only.

In groups with multiple Hermes bots, `telegram.exclusive_bot_mentions` keeps routing deterministic. When a message is addressed to one or more Telegram bot usernames, only the mentioned bot profiles process it; other Hermes bots ignore it. A message is addressed to another bot only when it starts with that bot's handle (`@OtherBot do this`, `@BotA @BotB: compare`, `/status@OtherBot`). A handle later in the text, as in `the shop has its own bot now (@ShopBot)`, only talks about that bot and does not silence this one. This bot's own handle anywhere still wins unless an opening command, optionally after this bot's address, explicitly targets another bot.

Broadcast **channel** posts run the same `allowed_chats`, `require_mention` and topic gates as groups, but never get the guest bypass: anyone with posting rights in a channel could otherwise cross the allowlist. Add the channel's ID to `allowed_chats` and @mention the bot in a post to address it.

### GuestBot

Guest Mode lets Telegram deliver a user's message plus any reference excerpts Telegram provides to the bot. Hermes processes these updates with its normal AI message handler; the moment a query is taken, it sends a status message replying to the guest's message, then edits that message in place with the answer (falling back to Telethon's native Guest Mode result method when the status message cannot be sent or edited). Guest conversations use a separate per-user thread rather than the ordinary group transcript; only the request and Telegram-supplied excerpts are available. Existing Hermes authorization and allowlist checks remain in force. Telegram Guest Mode must also be enabled for the bot in BotFather.

Open `/help` and choose **GuestBot settings**, or send `/guestbot`, `/guestbot on`, or `/guestbot off`. The interactive toggle is saved in the active profile's `platforms.telegram.guest_mode` setting. GuestBot is enabled by default; set `guest_mode: false` to disable it in `config.yaml`. This setting controls Hermes' local processing only and does not change BotFather configuration.

Each guest's conversation is kept in its own session under the owner's profile, separate from the owner's own chats, and it grows like any other session. **Save guest messages** (in the same GuestBot settings page, saved as `platforms.telegram.guest_save_history`, on by default) controls whether those sessions are kept: turn it off and a guest turn is ephemeral — nothing is saved, and the next message from that guest starts fresh. **Clear guest history** (owner-only, in the same page) deletes every stored guest session in the profile at once, after a confirm card that names how many sessions and messages will go; a guest session with a turn still running is skipped, never deleted. When GuestBot itself is off, strangers get no reply at all — the bot silently ignores them.

### Streaming response (Beta)

**Telegram DM only.** When the Beta **Streaming response** toggle is on in `/settings` (Personal settings), the bot streams its answer as a live rich draft in a private chat: reasoning shows inside the draft first, then the full content renders as it arrives, and the complete rich message is sent as the final reply. Groups always get normal replies — there is no edit-loop streaming anywhere. The toggle is saved in the active profile's `platforms.telegram.streaming_response` setting and is off by default; set `streaming_response: true` to enable it in `config.yaml`. A draft is only a preview (it expires on its own in about 30 seconds), so a turn whose preview fails still delivers its final answer normally.

### Threaded replies (Beta)

When the Beta **Threaded replies** toggle is on in `/settings` (Personal settings), a turn that answers with several messages chains them: the first replies to your message as usual, and each following one replies to the message just before it instead of quoting yours again. A one-message answer behaves exactly as before. The toggle is saved in the active profile's `platforms.telegram.threaded_replies` setting and is off by default; set `threaded_replies: true` to enable it in `config.yaml`.

### Message edits (Beta)

When the Beta **Message edits** toggle is on in `/settings` (Personal settings), editing a message you already sent starts a new turn automatically with the corrected text: Hermes replies "I saw your edit." (that message deletes itself shortly after), then answers the corrected text. If Hermes already replied to the original, the corrected answer replaces that reply in place; if the correction changes nothing, the previous reply stands and nothing new is sent. The toggle is saved in the active profile's `platforms.telegram.message_edits` setting and is off by default; set `message_edits: true` to enable it in `config.yaml`. Edits are handled under the normal admission rules for whoever sent the message, exactly like a follow-up.

### Acknowledgement fast path (Beta)

When the Beta **Acknowledgement fast path** toggle is on in `/settings` (Personal settings), a message that is only an acknowledgement — a bare "thanks", "ok", "chalo", "theek hai", or a thumbs-up emoji — gets a 👍 reaction on your message and no agent turn runs, so no model call is spent. Anything else runs the full turn unchanged: questions, tasks, commands, media, an "ok" answering a pending approval or clarify question, and an "ok" replying to a question the agent asked. The toggle is saved in the active profile's `platforms.telegram.ack_fast_path` setting and is off by default; set `ack_fast_path: true` to enable it in `config.yaml`. The fast path runs after admission, so it never bypasses who may talk to the bot.
### Footer badges (Beta)

When the Beta **Footer badges** toggle is on in `/settings` (Personal settings), the runtime footer (`/footer`) renders as tappable badges instead of plain text: the same fields in the same order — model, context percent, working directory — each one a badge. Tapping a badge answers a short toast with the detail behind it: the model badge names the provider and the model's full name, the context badge shows the actual counts ("38,400 tokens of 256,000 tokens"), and the directory badge shows the full absolute path. The badges carry no guard of any kind — anyone who can see the message can tap one and get the answer, owner or not, in a DM or a group. The toggle is saved in the active profile's `platforms.telegram.footer_badges` setting and is off by default; set `footer_badges: true` to enable it in `config.yaml`. When the toggle is off, the footer is exactly the plain text line it always was. While the Beta toggle is on, a **New style** toggle appears just below it in `/settings`: turning it on renders the footer badges as link-style buttons instead of the primary style, and turning it off goes back. The toggle is saved in the active profile's `platforms.telegram.footer_badges_link_style` setting and is off by default. The footer itself stays off by default (`/footer on` enables it); the badges only change how an enabled footer renders.

### Model picker filters (Beta)

When the Beta **Model picker filters** toggle is on in `/settings` (Personal settings), the model picker for a provider gains a **Filter** button at the bottom of the card. Tapping it opens the filter buttons: **Free** (only free models) and the context-window buckets **256k**, **512k**, **512k+**, **1M**, **2M**. Tapping a filter edits the card in place so only the matching models are listed; tapping it again (or **Clear filter**) restores the full list. One filter applies at a time — a second tap replaces the first — and the filter composes with the search box. The filter is scoped to the provider currently open and never shows models from another provider. Models with an unknown context window match no bucket. The toggle is saved in the active profile's `platforms.telegram.model_picker_filters` setting and is off by default; set `model_picker_filters: true` to enable it in `config.yaml`. When the toggle is off, the picker is exactly as before, with no Filter button.

### Shared contacts and locations

A contact card or location pin you share with the bot reaches the agent as a short text block (name, phone number and vCard for a contact; coordinates and venue for a location), so the agent can act on it.

### Voice Messages

Voice notes you send on Telegram are downloaded over MTProto — there is no
Bot API `getFile` step, so the 20 MB public Bot API ceiling does not apply.
Inbound media is capped by `gateway.max_inbound_media_bytes` instead
(default 128 MB); a file over the cap is omitted with a short note rather
than transcribed.

#### Incoming Voice (Speech-to-Text)

Voice notes are automatically transcribed by the configured STT provider and
the transcript is injected as text into the conversation. The agent sees the
words, not the audio.

- `local` uses `faster-whisper` on the machine running Hermes — no API key required
- `groq` uses Groq Whisper and requires `GROQ_API_KEY`
- `openai` uses OpenAI Whisper and requires `VOICE_TOOLS_OPENAI_KEY` (or `OPENAI_API_KEY`)

Other providers (`mistral`, `xai`, `elevenlabs`, `deepinfra`, and custom
command providers) work the same way; see [Voice & TTS](../features/tts.md)
for the full list and configuration.

A plain audio file (`.mp3`, `.m4a`, …) is *not* auto-transcribed — only voice
notes enter the STT pipeline. The agent still receives the cached file path
and can transcribe or process it with a tool when asked.

`stt.echo_transcripts` (default `true`) sends the recognized transcript back
as a separate message so you can check transcription quality in real time.
`stt.language` sets the language hint; leave it blank for auto-detect.

#### Skipping STT: pass the raw audio file to the agent

If you'd rather have the **agent itself** handle audio — for diarization, a
custom transcription tool, or just archiving the recording — set
`stt.enabled: false` in `~/.hermes/config.yaml`:

```yaml
stt:
  enabled: false
```

With STT disabled, Hermes still downloads the voice note into its audio
cache, but **does not transcribe it**. The agent receives the message with a
marker like:

```
[The user sent a voice message: /home/<user>/.hermes/cache/audio/<hash>.ogg (duration: 0:42)]
```

Your tools or skills can then read that path directly (e.g., hand it off to a
local diarization pipeline, a richer transcription model, or upload it to
long-term storage). The file extension reflects the original format Telegram
delivered (`.ogg` for voice notes, `.mp3`/`.m4a`/etc. for audio attachments).

If transcription fails for a voice note, the agent gets a neutral marker
instead — `[voice message could not be transcribed automatically; the audio
is available at: <path>]` — and no separate failure notice is sent.

#### Outgoing Voice (Text-to-Speech)

When the agent generates audio via TTS, it is delivered as a native Telegram
**voice bubble** — the round, inline-playable kind — sent with Telethon's
`voice_note` flag. TTS providers that emit MP3 or WAV (Edge TTS, Piper, some
OpenAI-compatible servers) are transcoded to Ogg/Opus with **ffmpeg** first;
without ffmpeg the file is delivered in its real format as a regular audio
file instead of a voice bubble:

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

MP3 and M4A files go out as Telegram audio tracks. Their captions get the same markdown formatting as voice notes. With ffmpeg installed, the file's own artist and title tags and its embedded cover art also fill in the performer, track name and album art in Telegram's music player.

Configure the TTS provider in `config.yaml` under the `tts.provider` key; see
[Voice & TTS](../features/tts.md) for all eleven providers. The agent can
also send any audio file as a voice bubble by adding `[[audio_as_voice]]` on
its own line — non-Opus audio is transcoded automatically.

#### Spoken replies (`/voice`)

Send `/voice on` in a chat and the bot speaks its replies only when you sent
voice; `/voice tts` speaks every reply; `/voice off` returns to text only.
The choice is stored per chat. For the full walkthrough, see
[Use Voice Mode with Hermes](../../guides/use-voice-mode-with-hermes.md).

In a group, a voice note still has to address the bot (mention, reply, or
`/command`) to be answered. A captionless voice note that does not address it
gets the short notice described in [Staying silent in a group](#staying-silent-in-a-group),
because its words are not in the message text.

The Voice section of `hermes gateway setup` exposes `stt.enabled`,
`stt.echo_transcripts`, `stt.language`, `tts.provider`, `tts.edge.voice`, and
`tts.openai.voice`.

### Albums

Telegram delivers an album (several photos or files sent together) as one update per item.
Hermes waits until every item that has arrived is downloaded, then sends the whole album to the
agent as one message. It waits at most 8 seconds for a slow download before sending what it has.
After the last item, it waits a short debounce (default 1.5 s, at most 10 s) for one sent later.
Raise the debounce if your phone uploads album items far apart:

```yaml
platforms:
  telegram:
    extra:
      media_group_wait_seconds: 3.0
```

A short text followed right away by a photo (a question, then the screenshot it is about) also
reaches the agent as one message. If the photo starts arriving before the text has been sent on,
the text waits up to 8 seconds for the photo's download and joins it.

### Multi-session DM mode (`/topic`)

**Telegram DM only.** `/topic` manages user-managed multi-session topic mode in a private chat with the bot: `/topic` enables it or shows status, `/topic off` disables it and clears bindings, `/topic help` shows usage, and `/topic <session-id>` inside a topic restores a previous session. See the `/topic` row in the [slash-command reference](../reference/slash-commands.md) for the full description.

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
- **Messages are ignored in a group:** verify the sender/chat allowlist and mention requirement; with mention gating enabled, mention the bot or reply to one of its messages. If the bot answers in a private chat but stays silent in a group, check Telegram delivery first: turn off privacy mode in BotFather (`/setprivacy`), promote the bot to admin, or mention it directly — Hermes cannot respond to group messages Telegram never delivers. After changing privacy settings, remove the bot from the group and add it again; Telegram may keep the old delivery behavior for existing memberships.
- **Session file access:** stop the gateway before moving a profile. Do not delete or copy the session while Hermes is running.

For the protocol distinction and bot sign-in requirements, see Telethon's [Bot API vs. MTProto guide](https://docs.telethon.dev/en/stable/concepts/botapi-vs-mtproto.html) and [sign-in documentation](https://docs.telethon.dev/en/stable/basic/signing-in.html).
