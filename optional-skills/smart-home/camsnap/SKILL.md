---
name: camsnap
description: "Capture frames or clips from cameras via camsnap."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Camera, RTSP, ONVIF, Webcam, Smart-Home]
    category: smart-home
prerequisites:
  commands: [camsnap]
---

# camsnap Skill

Grab snapshots, clips, or motion events from configured cameras with the `camsnap` CLI: RTSP/ONVIF network cameras and local webcams, including USB pan/tilt/zoom control. Requires the `camsnap` binary, `ffmpeg` on `PATH`, and a configured camera.

## When to Use

- User asks for a snapshot or short clip from a named camera
- Listing or discovering cameras on the network
- Watching a camera for motion
- Moving a PTZ camera or checking its position

## Prerequisites

- Install `camsnap`: `brew install steipete/tap/camsnap`
- `ffmpeg` on `PATH`.
- Cameras configured in `~/.config/camsnap/config.yaml` (add one with `camsnap add`, see below).
- PTZ needs macOS Camera permission: these cameras only service UVC controls while streaming, so `camsnap` holds a capture session open for the operation.

## How to Run

1. Discover or list cameras with the `terminal` tool.
2. Prefer a short test capture before longer clips.
3. Save captures with `--out` and report the file path back with `read_file` or `vision_analyze` as needed.

## Quick Reference

```bash
camsnap discover --info
camsnap add --name kitchen --host 192.168.0.10 --user user --pass pass
camsnap snap kitchen --out shot.jpg
camsnap clip kitchen --dur 5s --out clip.mp4
camsnap watch kitchen --threshold 0.2 --action '...'
camsnap doctor --probe
```

Local webcams (macOS):

```bash
camsnap devices
camsnap snap --device 0 --out webcam.jpg
camsnap ptz status --device 0
camsnap ptz goto --device 0 --pan 45 --tilt -18
camsnap ptz move --device 0 --pan 5 --tilt 0
camsnap ptz home --device 0
```

## Procedure

1. Find the camera: `camsnap discover --info` for network cameras, `camsnap devices` for local webcams. If the named camera is missing, run `camsnap doctor --probe` before anything else.
2. Snapshot: `camsnap snap <name> --out <file>`. Clip: `camsnap clip <name> --dur <5s> --out <file>`. Start short; extend only after a short capture works.
3. Motion: `camsnap watch <name> --threshold <0.2> --action '<command>'`.
4. PTZ: check `camsnap ptz status --device <n>` first, then `camsnap ptz goto` for an absolute position or `camsnap ptz move` for relative deltas, `camsnap ptz home` to reset.
5. Report the saved file path for every capture.

## Pitfalls

- A camera password passed on the command line lands in shell history. Prefer adding the camera once with `camsnap add` (stored in config) over passing `--pass` every time, and never print the password.
- Motion commands verify the settled position and exit non-zero if the camera did not reach it. A non-zero exit after a PTZ move is a real failure, not noise.
- On-camera AI framing can override manual positioning. If moves keep missing, disable tracking on the camera before retrying.
- Clips cost disk and time. Confirm the duration with the user for anything longer than a few seconds.
- If a snapshot comes back empty or stale, re-run `camsnap doctor --probe` and check the camera is reachable before retrying the capture.

## Verification

- After a snapshot, confirm the output file exists and has non-zero size, and view it with `vision_analyze` when the user asks what the camera sees.
- After a clip, confirm the duration and file size are plausible for what was asked.
- After a PTZ move, run `camsnap ptz status` and confirm the reported position matches the requested one.
