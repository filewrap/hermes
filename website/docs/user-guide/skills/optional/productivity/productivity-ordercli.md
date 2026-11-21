---
title: "Ordercli — Check Foodora orders and track active deliveries"
sidebar_label: "Ordercli"
description: "Check Foodora orders and track active deliveries"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Ordercli

Check Foodora orders and track active deliveries.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/productivity/ordercli` |
| Path | `optional-skills/productivity/ordercli` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `Foodora`, `Orders`, `Delivery`, `Food` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# ordercli Skill

Check past orders and track active order status with the `ordercli` CLI. Foodora only right now; Deliveroo support is work in progress and does not work yet. Requires the `ordercli` binary and a Foodora account.

## When to Use

- User asks where their Foodora order is, what they ordered before, or the status of an active delivery
- Listing Foodora order history or showing one past order in detail
- Reordering a past Foodora order into the cart

## Prerequisites

- Install `ordercli`: `brew install steipete/tap/ordercli`, or `go install github.com/steipete/ordercli/cmd/ordercli@latest`
- A Foodora account, logged in with `ordercli foodora login` (see Procedure).
- Run commands with the `terminal` tool. Use `--config /tmp/ordercli.json` for testing so test runs do not touch the real config.

## How to Run

1. Set the country once: `ordercli foodora config set --country AT` (or the user's country code).
2. Log in if not already logged in. Prefer session import over typing a password.
3. List orders, show history, or reorder as asked.

## Quick Reference

```bash
ordercli foodora countries
ordercli foodora config set --country AT
ordercli foodora login --email you@example.com --password-stdin
ordercli foodora orders
ordercli foodora history --limit 20
ordercli foodora history show <orderCode>
```

Active order detail and watch:

```bash
ordercli foodora orders --watch
ordercli foodora order <orderCode>
ordercli foodora history show <orderCode> --json
```

Reorder (adds to cart):

```bash
ordercli foodora reorder <orderCode>
ordercli foodora reorder <orderCode> --confirm
ordercli foodora reorder <orderCode> --confirm --address-id <id>
```

## Procedure

1. Confirm the country is set: `ordercli foodora countries` lists the supported ones, then `ordercli foodora config set --country <code>`.
2. Log in. Prefer session import with no password: `ordercli foodora session chrome --url https://www.foodora.at/ --profile "Default"`, then `ordercli foodora session refresh --client-id android` if the session is stale. Otherwise log in with `ordercli foodora login --email you@example.com --password-stdin`, feeding the password on stdin, never as a visible argument and never printed.
3. If login hits Cloudflare or bot protection, retry with the browser login: `ordercli foodora login --email you@example.com --password-stdin --browser`, reusing the profile with `--browser-profile "$HOME/Library/Application Support/ordercli/browser-profile"`, or import Chrome cookies with `ordercli foodora cookies chrome --profile "Default"`.
4. Active orders: `ordercli foodora orders`, or `ordercli foodora orders --watch` to follow one live. Detail: `ordercli foodora order <orderCode>`.
5. History: `ordercli foodora history --limit 20`, detail with `ordercli foodora history show <orderCode> --json`.
6. Reorder: preview with `ordercli foodora reorder <orderCode>` first; only pass `--confirm` (and `--address-id` if needed) when the user confirms. A reorder adds to the cart; it does not place the order.

### Deliveroo (work in progress, not working yet)

- Requires `DELIVEROO_BEARER_TOKEN` (optional `DELIVEROO_COOKIE`). Keys live in the Hermes `.env` (secrets only); never print them.
- `ordercli deliveroo config set --market uk`
- `ordercli deliveroo history`

Do not present Deliveroo as working. If the user asks for it, say it is not working yet.

## Pitfalls

- Foodora only. Do not claim other delivery services work.
- Never print a password, bearer token, or cookie. Passwords go via `--password-stdin` only.
- A reorder with `--confirm` changes the cart. Preview without `--confirm` first and confirm with the user before confirming.
- A stale session looks like a login failure. Refresh the session (`ordercli foodora session refresh --client-id android`) before retrying a login.
- Browser login needs a GUI browser profile. On a headless host it cannot run; use session import instead.

## Verification

- After login, run `ordercli foodora orders` and confirm it returns without an auth error.
- After showing an order, confirm the order code, restaurant, items, and status match what the user asked about.
- After a reorder preview, confirm the cart contents with the user before passing `--confirm`.
