---
title: "Telegram Sessions — Log in, run, and maintain many Telegram accounts"
sidebar_label: "Telegram Sessions"
description: "Log in, run, and maintain many Telegram accounts"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Telegram Sessions

Log in, run, and maintain many Telegram accounts.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/messaging/telegram-sessions` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `telegram`, `sessions`, `login`, `accounts`, `messaging` |
| Related skills | [`telegram-client`](../../bundled/messaging/messaging-telegram-client.md), [`telegram-bots`](../../bundled/messaging/messaging-telegram-bots.md), [`telegram-rich-messages`](../../bundled/messaging/messaging-telegram-rich-messages.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Telegram Sessions Skill

Operate Telegram accounts as standalone clients: log in, keep sessions alive, and maintain many accounts side by side. This skill covers accounts the agent runs itself. It does not cover the gateway's own connected session — that session is governed by the telegram-client skill and the `telegram_client` tool.

## When to Use

Use when the user asks to log in to a Telegram account, check whether an account is logged in, recover an expired session, change a 2FA password, or run and maintain several Telegram accounts at once. Use the telegram-client skill for anything in the active chat on the gateway's connected session, and telegram-bots for managed bot inventories.

## Prerequisites

- Telethon 1.45.0, pinned by the repo (the Telegram adapter enforces the pin for gateway work through `require_pinned_telethon`; standalone scripts use the same installed wheel). All constructors and signatures below are from that wheel — do not write them from memory.
- A Telegram API id and API hash for the app the account logs in with. These are app credentials, not the account password: keep them in the profile's secret store (`.env` holds secrets only), never in chat, code, or files the user can see.
- The own-session rule: never perform direct or script operations on the gateway's own connected session — no `TelegramClient`, no `client.start`, no raw MTProto calls against it; for the own session use only the `telegram_client` tool. This skill is for other accounts.
- Codes and passwords arrive only through the approved secure path. Never ask the user to paste a login code or 2FA password into chat, and never store raw credentials in files, memory, or code. Session files hold an authorization key, not a password — protect them like credentials (see Procedure).
- The gateway adapter itself accepts bot accounts only and refuses non-bot accounts, so user accounts always run as standalone clients outside the gateway.

## How to Run

1. **Identify the account and the library.** One account, one client, one session file. Name the session after the account so accounts never share a file.
2. **Check state first.** Connect and call `is_user_authorized()` before logging in — an existing session is usually still valid.
3. **Log in only when unauthorized.** Run the code step, then the 2FA step when Telegram asks for it. Hand codes and passwords through the secure path only.
4. **Verify.** Call `get_me()` and report the account's id, username, and whether it is a bot or a user account.
5. **Store nothing but the session.** Keep the session file; discard codes and passwords the moment login succeeds.
6. **Run standalone clients as scripts.** Create a script with `patch` and run it with the `terminal` tool; read results back with `read_file`. Never run a standalone client inside the gateway turn's own session.

## Quick Reference

| Need | Call | Notes |
| --- | --- | --- |
| New client (file session) | `TelegramClient("name", api_id, api_hash)` | Creates `name.session` on first connect; one live client per file. |
| New client (string session) | `TelegramClient(StringSession(), api_id, api_hash)` | Call `client.session.save()` after login; store the string in the secret store. |
| Check login state | `await client.is_user_authorized()` | `True` means the saved session is still valid. |
| Interactive login | `await client.start(phone=..., password=...)` | `code_callback` supplies the login code; `password` supplies 2FA. |
| Manual login | `send_code_request` then `sign_in` | See Procedure. |
| Bot login | `await client.start(bot_token=...)` | Or `sign_in(bot_token=...)`. |
| Who am I | `await client.get_me()` | Returns the account's user/bot object. |
| Log out (revoke session) | `await client.log_out()` | Revokes the authorization; the session file becomes unusable. |
| Disconnect | `await client.disconnect()` | Keeps the session; reconnect with `connect()`. |
| Change 2FA | `await client.edit_2fa(...)` | See Procedure. |

## Procedure

### Create and load a session

- File sessions: `TelegramClient("accounts/<name>", api_id, api_hash)` — the first `connect()` creates the session file. Keep the directory private (mode `0o700`, file `0o600`), the same protection the gateway adapter applies to its own session file.
- String sessions: `TelegramClient(StringSession(), api_id, api_hash)`; after a successful login, save the string with `client.session.save()` and store it in the secret store. A string session is portable across machines; a file session is not.
- Never open the same session file with two live clients at once. One live client per session file; a second client on the same file corrupts or expires it. This is a library limit, not a Hermes rule.
- The gateway's own session lives per profile home under the profile's sessions directory (`bot.session`) and is owned by the gateway adapter alone — never open it from a script.

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
- Otherwise request a code: `sent = await client.send_code_request(phone)`; the code goes to the account's Telegram app or SMS. The `phone_code_hash` on the returned object belongs to this attempt — pass it back to `sign_in`.
- Complete the code step: `await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)`. The code and the hash are single-use for this attempt.
- If Telegram raises `SessionPasswordNeededError`, the account has two-step verification: complete it with `await client.sign_in(password=...)`. There is no separate password-check method in Telethon 1.45.0 — the 2FA password goes in `sign_in`.
- Prefer the interactive form when a script may run unattended: `await client.start(phone=..., password=..., code_callback=...)` performs connect, code, and 2FA in one call, prompting through the callbacks.
- Bot accounts log in with a token instead: `await client.start(bot_token=...)`. No code or password is involved.

### Session states

- **Authorized:** `is_user_authorized()` is `True`. Operate normally.
- **Unauthorized (fresh or signed out):** `is_user_authorized()` is `False` and no code has been entered. Run the login flow.
- **Expired / revoked:** Telegram raises `AuthKeyUnregisteredError` or `SessionRevokedError`, or the account shows signed out after working before. The saved authorization is dead — delete the session file (or clear the stored string) and log in again. Do not retry the old session.
- **Flood-waited:** Telegram raises `FloodWaitError` with a `seconds` value. Wait it out — the wait is enforced by Telegram. Telethon sleeps automatically for waits under its `flood_sleep_threshold` (60 seconds by default) and raises for longer ones; never hammer retries to shorten it.
- **Banned / deactivated:** `UserDeactivatedBanError` (or the account is banned on login). No client call recovers it — report it and stop. Do not create a replacement account to evade a ban.

### Password and 2FA

- The login code is a one-time code Telegram sends to the account's app or phone. The 2FA password is the account's two-step verification password. They are different secrets and are entered at different steps (code first, password only when `SessionPasswordNeededError` is raised).
- Both arrive through the approved secure path only — the same path the vault uses. The vault can mint a current one-time code from a stored TOTP seed (`resolve_otp` in the vault backends; server-side only, returns `None` when no seed is stored and the user is asked instead). That covers services that store a TOTP seed; Telegram's own login code and 2FA password always come from the user through the secure path.
- On a headless gateway host, Hermes Desktop shows the profile's graphical screen live and lets a human take over for logins, 2FA, or CAPTCHAs, then hand control back.
- Never write a code or password to a file, a log, a chat message, or a session file. After login succeeds, keep only the session file (or the saved string session).
- Change a 2FA password with `client.edit_2fa(current_password=..., new_password=..., hint=..., email=...)`. Removing 2FA needs the current password.

### Maintain 10+ accounts

- No code cap on the number of accounts was found: the only barrier was the old policy text this skill replaces. The limits that exist are Telegram's own (flood limits, active-session limits per account, one API id/hash per app). For other accounts the agent has complete control: `TelegramClient`, `client.start`, scripts, `execute_code`, and high- and low-level Telethon operations are all allowed.
- One `TelegramClient` instance per account, one session file (or one stored string) per account, named after the account. Never share session files between accounts or between a script and the gateway.
- Keep a registry: account label, phone/username, bot-or-user, session location, library in use, and last-verified date. Verify each account with `is_user_authorized()` plus `get_me()` before operating it; an account that fails verification goes through the login flow, not through another account's session.
- Different libraries: Telethon is the bundled, pinned library for Hermes work. Another MTProto library may operate an account only when it is installed and the user asked for it by name — do not install or invent one. Accounts stay independent: a failure in one account's client never touches another account's session.
- The gateway holds one client per profile (bot-token only); profiles are the isolation unit there. Standalone user accounts run as scripts outside the gateway, one process per account or one process holding one client per account.

## Pitfalls

- Never touch the gateway's own session from a script — not even to read it. Use the `telegram_client` tool for the own session.
- Never reuse a `phone_code_hash` across attempts or accounts; it belongs to one code request.
- Never pass a login code or 2FA password as a plain argument in a shared command or a log line. Use the secure path and the `code_callback` / `password` parameters.
- Do not call `log_out()` to "refresh" a session — it revokes the authorization. Disconnect and reconnect instead.
- A session file copied to another machine works only if the same API id/hash is used and Telegram accepts the authorization; a copied file is still a credential — protect it.
- Flood waits are per account and per method. A wait on one account does not block other accounts, but hammering retries on the waited account extends the problem.

## Verification

- After login, `is_user_authorized()` returns `True` and `get_me()` returns the expected account (id and username match the registry).
- After any session operation, re-check state: authorized, and the session file (or stored string) is present and private.
- Report per account: label, state, and the last action taken. A failed login reports the exact Telegram error name, never a bare "failed".
