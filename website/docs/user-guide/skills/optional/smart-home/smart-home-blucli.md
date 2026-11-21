---
title: "Blucli — Control Bluesound and NAD players via the blu CLI"
sidebar_label: "Blucli"
description: "Control Bluesound and NAD players via the blu CLI"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Blucli

Control Bluesound and NAD players via the blu CLI.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/smart-home/blucli` |
| Path | `optional-skills/smart-home/blucli` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `BluOS`, `Bluesound`, `Audio`, `Smart-Home`, `Music` |
| Related skills | [`openhue`](../../optional/smart-home/smart-home-openhue.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# blucli Skill

Control Bluesound and NAD players with the `blu` CLI: discovery, playback, grouping, and volume. Single-vendor skill: it only speaks BluOS devices, on the local network, with the `blu` binary installed.

## When to Use

- User asks to play, pause, stop, or change volume on a Bluesound or NAD player
- Discovering BluOS devices on the network
- Grouping players or playing TuneIn radio on a BluOS device

## Prerequisites

- A BluOS device (Bluesound or NAD player) on the same local network.
- Install `blu`: `go install github.com/steipete/blucli/cmd/blu@latest`
- Run commands with the `terminal` tool. Prefer `--json` output when scripting.

## How to Run

1. Discover devices with `terminal`: `blu devices`, and pick the target.
2. Confirm the target device before changing playback.
3. Run the playback, volume, or grouping command against that device.

## Quick Reference

```bash
blu devices
blu --device <id> status
blu play
blu pause
blu stop
blu volume set 15
```

Target selection, in priority order:

- `--device <id|name|alias>`
- `BLU_DEVICE`
- Config default, if set

## Procedure

1. Run `blu devices` and identify the target by id, name, or alias. If several players answer, ask the user which one.
2. Check state first: `blu --device <id> status`.
3. Playback: `blu play`, `blu pause`, or `blu stop` against the confirmed device.
4. Volume: `blu volume set <0-100>` against the confirmed device.
5. Grouping: `blu group status`, `blu group add`, or `blu group remove`.
6. TuneIn: `blu tunein search "query"`, then `blu tunein play "query"`.
7. Report back the device name and the new state.

## Pitfalls

- Never change playback on an unconfirmed device. With several players on the network, a bare `blu play` can hit the wrong room.
- The device must be reachable on the local network. A player on a different subnet or VLAN will not appear in `blu devices`.
- Volume is a plain number; confirm the scale with the user for large jumps instead of assuming.
- If a command fails, re-run `blu devices` first: the player may have moved, renamed, or left the network.

## Verification

- After a playback change, run `blu --device <id> status` and confirm the state matches what was asked.
- After a volume change, confirm the reported level in the status output.
- After grouping, run `blu group status` and confirm the members match.
