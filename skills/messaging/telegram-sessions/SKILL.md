---
name: telegram-sessions
description: Log in, run, and maintain many Telegram accounts.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [telegram, sessions, login, accounts, messaging]
    category: messaging
    related_skills: [telegram-client, telegram-bots, telegram-rich-messages]
---

# Telegram Sessions Skill

Operate Telegram accounts as standalone clients: log in, keep sessions alive, and maintain many accounts side by side. This skill covers accounts the agent runs itself. It does not cover the gateway's own connected session — never do direct or script operations on that session; use the `telegram_client` tool for it.

## When to Use

Use when the user asks to log in to a Telegram account, check whether an account is logged in, recover an expired session, change a 2FA password, run and maintain several Telegram accounts at once, join a voice chat from an account, register a trigger on an account, or run requests directly on an account. Use the telegram-client skill for anything in the active chat on the gateway's connected session, and telegram-bots for managed bot inventories.

## Prerequisites

- Telethon 1.45.0, pinned by the repo (the Telegram adapter enforces the pin for gateway work through `require_pinned_telethon`; standalone scripts use the same installed wheel). All constructors and signatures below are from that wheel — do not write them from memory.
- A Telegram API id and API hash for the app the account logs in with. Use the already-configured api_id and api_hash when the user does not provide one; ask the user when neither is configured.
- Login codes and 2FA passwords come from the user: ask for the OTP and the 2FA password directly when the login flow asks for them.
- The gateway adapter runs bot accounts, so user accounts run as standalone clients outside the gateway.

## How to Run

1. **Identify the account and the library.** One account, one client, one session file. Name the session after the account so accounts never share a file.
2. **Check state first.** Connect and call `is_user_authorized()` before logging in — an existing session is usually still valid.
3. **Log in only when unauthorized.** Run the code step, then the 2FA step when Telegram asks for it. Ask the user for the code and the password directly.
4. **Verify.** Call `get_me()` and report the account's id, username, and whether it is a bot or a user account.
5. **Store nothing but the session.** Keep the session file; discard codes and passwords the moment login succeeds.
6. **Run standalone clients as scripts.** Create a script with `patch` and run it with the `terminal` tool; read results back with `read_file`.

## Quick Reference

| Need | Call | Notes |
| --- | --- | --- |
| New client (file session) | `TelegramClient("name", api_id, api_hash)` | Creates `name.session` on first connect; one live client per file. |
| New client (string session) | `TelegramClient(StringSession(), api_id, api_hash)` | Call `client.session.save()` after login; keep the saved string with the account record. |
| Check login state | `await client.is_user_authorized()` | `True` means the saved session is still valid. |
| Interactive login | `await client.start(phone=..., password=...)` | `code_callback` supplies the login code; `password` supplies 2FA. |
| Manual login | `send_code_request` then `sign_in` | See Procedure. |
| Bot login | `await client.start(bot_token=...)` | Or `sign_in(bot_token=...)`. |
| Who am I | `await client.get_me()` | Returns the account's user/bot object. |
| Log out (revoke session) | `await client.log_out()` | Revokes the authorization; the session file becomes unusable. |
| Disconnect | `await client.disconnect()` | Keeps the session; reconnect with `connect()`. |
| Change 2FA | `await client.edit_2fa(...)` | See Procedure. |
| Direct TL request | `await client(request)` | Any Telethon TL request object, run on the account. |
| Voice chat | `PyTgCalls(client)` | See Voice chats. |

## Procedure

### Create and load a session

- File sessions: `TelegramClient("accounts/<name>", api_id, api_hash)` — the first `connect()` creates the session file.
- String sessions: `TelegramClient(StringSession(), api_id, api_hash)`; after a successful login, save the string with `client.session.save()` and keep it with the account record. A string session is portable across machines; a file session is not.
- Never open the same session file with two live clients at once. One live client per session file; a second client on the same file corrupts or expires it. This is a library limit, not a Hermes rule.
- The gateway's own session lives per profile home under the profile's sessions directory (`bot.session`) and is owned by the gateway adapter.

```python
from telethon import TelegramClient
from telethon.sessions import StringSession

# File session: one file per account, named after the account.
client = TelegramClient("accounts/main", api_id, api_hash)
await client.connect()
if not await client.is_user_authorized():
    await client.start(phone=phone, code_callback=code_callback, password=password)
```

### Log in a user account

- Connect first: `await client.connect()`.
- If `await client.is_user_authorized()` is `True`, the session is valid — skip login.
- Otherwise request a code: `sent = await client.send_code_request(phone)`; the code goes to the account's Telegram app or SMS. Ask the user for the code directly. The `phone_code_hash` on the returned object belongs to this attempt — pass it back to `sign_in`.
- Complete the code step: `await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)`. The code and the hash are single-use for this attempt.
- If Telegram raises `SessionPasswordNeededError`, the account has two-step verification: ask the user for the 2FA password and complete it with `await client.sign_in(password=...)`. There is no separate password-check method in Telethon 1.45.0 — the 2FA password goes in `sign_in`.
- Prefer the interactive form when a script may run unattended: `await client.start(phone=..., password=..., code_callback=...)` performs connect, code, and 2FA in one call, prompting through the callbacks.
- Bot accounts log in with a token instead: `await client.start(bot_token=...)`. No code or password is involved.

### Session states

- **Authorized:** `is_user_authorized()` is `True`. Operate normally.
- **Unauthorized (fresh or signed out):** `is_user_authorized()` is `False` and no code has been entered. Run the login flow.
- **Expired / revoked:** Telegram raises `AuthKeyUnregisteredError` or `SessionRevokedError`, or the account shows signed out after working before. The saved authorization is dead — delete the session file (or clear the stored string) and log in again. Do not retry the old session.
- **Flood-waited:** Telegram raises `FloodWaitError` with a `seconds` value. Wait it out — the wait is enforced by Telegram. Telethon sleeps automatically for waits under its `flood_sleep_threshold` (60 seconds by default) and raises for longer ones.
- **Banned / deactivated:** `UserDeactivatedBanError` (or the account is banned on login). No client call recovers it — report it.

### Password and 2FA

- The login code is a one-time code Telegram sends to the account's app or phone. The 2FA password is the account's two-step verification password. They are different secrets and are entered at different steps (code first, password only when `SessionPasswordNeededError` is raised).
- Ask the user for each one directly, at its step. Codes and passwords are single-use.
- On a headless gateway host, Hermes Desktop shows the profile's graphical screen live and lets a human take over for logins, 2FA, or CAPTCHAs, then hand control back.
- After login succeeds, keep only the session file (or the saved string session).
- Change a 2FA password with `client.edit_2fa(current_password=..., new_password=..., hint=..., email=...)`. Removing 2FA needs the current password.

### Session discipline

- One live client per session file. `connect()` once, then keep the client running for as long as the account is in use. Do not connect repeatedly — repeated connects are what get an account flood-waited.
- Keep sessions running. A running client holds the session; stopping and starting it over and over wears the account out. Maintain the online session and reuse it for every operation on that account.
- When new code needs a fresh start, restart — but space restarts out. Do not restart immediately after a restart, and never restart in a tight loop: stacked restarts trigger flood waits. If the change can run inside the live session, use eval in the live session instead of restarting.
- Flood waits are per account and per method. A wait on one account does not block other accounts. Wait the `seconds` Telegram gives, then continue; do not hammer the same call while waiting.

### Maintain 10+ accounts

- No code cap on the number of accounts was found: the only barrier was the old policy text this skill replaces. The limits that exist are Telegram's own (flood limits, active-session limits per account, one API id/hash per app). For other accounts the agent has complete control: `TelegramClient`, `client.start`, scripts, `execute_code`, and high- and low-level Telethon operations are all allowed.
- One `TelegramClient` instance per account, one session file (or one stored string) per account, named after the account. A session file holds one account's authorization — it is not shared between accounts.
- Keep a registry: account label, phone/username, bot-or-user, session location, library in use, and last-verified date. Verify each account with `is_user_authorized()` plus `get_me()` before operating it; an account that fails verification goes through the login flow, not through another account's session.
- Telethon is the bundled, pinned library for Hermes work. Another MTProto library works when it is installed — use the one the user names.
- The gateway holds one client per profile (bot-token only); profiles are the isolation unit there. Standalone user accounts run as scripts outside the gateway, one process per account or one process holding one client per account.

### Limited or flagged accounts

- If an account comes back limited or flagged (a restriction notice, a flood block, a spam block, or Telegram refusing an operation for that account), record it in the registry: account label, what was attempted, the exact Telegram error or restriction, and when it happened.
- Do not run that same thing on that account again until the user explicitly says to run it. Other accounts are unaffected — a flag on one account never blocks another.
- When the user says to retry, run it once and record the outcome the same way.

### Voice chats

- Voice-chat access runs on the user account through py-tgcalls by laky64 (`pip install py-tgcalls -U`; Python 3.10 or higher). It attaches to the account's Telethon client — one `PyTgCalls` per client.
- `from pytgcalls import PyTgCalls, idle`, then `app = PyTgCalls(client)`, `app.start()`, and join with `app.play(chat_id, "https://.../audio.mp3")` — a file path or URL; older releases use `app.join_group_call(chat_id, MediaStream(...))`, so check the method names on the installed version. `idle()` keeps the call process running.
- Controls: mute/unmute, pause/resume, stop/play, volume. Voice chats work in groups and channels; the account joins as itself or as a channel.
- Keep the client and the `PyTgCalls` instance running together for the whole call — the same session discipline applies: one live pair, no repeated connects.

### Custom triggers

- Register triggers on the user account the same way Hermes registers its own: event handlers on the live client that fire on matching updates.
- `@client.on(events.NewMessage(pattern=r"(?i)\btrigger word\b"))` runs a handler whenever the trigger word arrives; `events.NewMessage(chats=...)` scopes it to a chat. Handlers run inside the long-lived client process — register them once at startup and keep the process running.
- A trigger handler can do anything the account can do: reply, run a request, start a voice chat, hand the update to the agent.

### Direct requests

- High- and low-level requests run directly on the user account. See the request, build it, run it: `await client(request)` with any Telethon TL request object — `client.get_entity`, `client.send_message`, raw `functions.messages.*` / `functions.channels.*` calls, all of it.
- Build the request from the installed Telethon TL layer (`telethon.tl.functions`, `telethon.tl.types`) — do not write TL constructors from memory. The same client that holds the session runs the request; no second client, no extra login.

### Monitoring and running

- Monitor for new updates on the live client: register event handlers (`events.NewMessage`, `events.ChatAction`, `events.Raw`) and let the process run. Report new updates as they arrive.
- Run Hermes for the user client: a long-lived script holding the account's client is how the account stays online, watches updates, answers triggers, and runs requests. Keep it running; restart only as session discipline says (spaced, not immediate).
- Keep a run record per account: process, started time, last update seen, last action taken.

## Pitfalls

- A `phone_code_hash` belongs to one code request; reusing it across attempts or accounts fails.
- Do not call `log_out()` to "refresh" a session — it revokes the authorization. Disconnect and reconnect instead.
- A session file copied to another machine works only if the same API id/hash is used and Telegram accepts the authorization.
- Flood waits are per account and per method. A wait on one account does not block other accounts.
- Do not restart a live client in a tight loop — stacked restarts trigger flood waits. Use eval in the live session when possible.
- A limited or flagged account stays flagged until the user says to retry — do not re-run the same thing on it.
- A voice-chat join needs a live client and a live `PyTgCalls` attached to it; starting a second client on the same session file breaks the call.

## Verification

- After login, `is_user_authorized()` returns `True` and `get_me()` returns the expected account (id and username match the registry).
- After any session operation, re-check state: authorized, and the session file (or stored string) is present.
- Report per account: label, state, and the last action taken. A failed login reports the exact Telegram error name, never a bare "failed".
- After joining a voice chat, confirm the call state on the account (joined, playing) before reporting it.
- The registry records limited/flagged accounts with the attempt, the error, and the time — and nothing is re-run on them until the user says so.
