---
title: "Sonoscli — Control Sonos speakers: discover, play, volume, group"
sidebar_label: "Sonoscli"
description: "Control Sonos speakers: discover, play, volume, group"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Sonoscli

Control Sonos speakers: discover, play, volume, group.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/smart-home/sonoscli` |
| Path | `optional-skills/smart-home/sonoscli` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `Sonos`, `Speakers`, `Audio`, `Smart-Home`, `Music` |
| Related skills | [`openhue`](../../optional/smart-home/smart-home-openhue.md), [`blucli`](../../optional/smart-home/smart-home-blucli.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Sonos CLI Skill

Control Sonos speakers on the local network with the `sonos` CLI: discovery, status, playback, volume, grouping, favorites, and queue. Single-vendor skill: it only speaks Sonos speakers, with the `sonos` binary installed.

## When to Use

- User asks to play, pause, stop, or change volume on a Sonos speaker
- Discovering Sonos speakers on the network
- Grouping speakers, managing the queue, or opening a favorite
- Searching Spotify through a Sonos speaker (SMAPI)

## Prerequisites

- Sonos speakers on the same local network.
- Install `sonos`: `go install github.com/steipete/sonoscli/cmd/sonos@latest`
- Run commands with the `terminal` tool.
- Spotify search via SMAPI is optional and needs `SPOTIFY_CLIENT_ID` / `SPOTIFY_SECRET`.

## How to Run

1. Discover speakers with `terminal`: `sonos discover`, and pick the target by name.
2. Confirm the target speaker before changing playback.
3. Run the playback, volume, grouping, or queue command against that speaker.

## Quick Reference

```bash
sonos discover
sonos status --name "Kitchen"
sonos play --name "Kitchen"
sonos pause --name "Kitchen"
sonos stop --name "Kitchen"
sonos volume set 15 --name "Kitchen"
```

Grouping:

```bash
sonos group status
sonos group join
sonos group unjoin
sonos group party
sonos group solo
```

Favorites and queue:

```bash
sonos favorites list
sonos favorites open
sonos queue list
sonos queue play
sonos queue clear
```

Spotify search (via SMAPI):

```bash
sonos smapi search --service "Spotify" --category tracks "query"
```

If SSDP discovery fails, pass the speaker address directly with `--ip <speaker-ip>`.

## Procedure

1. Run `sonos discover` and identify the target speaker by name. If several answer, ask the user which one.
2. Check state first: `sonos status --name "<speaker>"`.
3. Playback: `sonos play`, `sonos pause`, or `sonos stop` with `--name "<speaker>"`.
4. Volume: `sonos volume set <0-100> --name "<speaker>"`.
5. Grouping: `sonos group join` / `unjoin` / `party` / `solo`, then `sonos group status` to confirm.
6. Queue: `sonos queue list` to see what is queued, `sonos queue play` / `clear` as asked.
7. Report back the speaker name and the new state.

## Pitfalls

- Never change playback on an unconfirmed speaker. With several speakers on the network, a command without `--name` can hit the wrong room.
- `sonos discover` needs local-network access. On macOS in direct (non-sandbox) mode, Settings -> Privacy & Security -> Local Network must be enabled for the top-level host parent process (Terminal, the gateway launcher, or the editor hosting the terminal). A sandbox that blocks network access fails the same way.
- `sonos discover` failing with `no route to host` on the SSDP multicast address (`239.255.255.250:1900`) means the network path is blocked, not that there are no speakers. Check Local Network permission or the sandbox network setting first.
- `bind: operation not permitted` on discovery means the process cannot bind UDP: same cause, same fix.
- If a command fails, re-run `sonos discover` first: the speaker may have moved, renamed, or left the network.

## Verification

- After a playback change, run `sonos status --name "<speaker>"` and confirm the state matches what was asked.
- After a volume change, confirm the reported level in the status output.
- After grouping, run `sonos group status` and confirm the members match.
