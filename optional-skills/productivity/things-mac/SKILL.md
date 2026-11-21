---
name: things-mac
description: "Manage Things 3 todos, inbox, and projects on macOS."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Things, Todos, macOS, Tasks]
    category: productivity
    related_skills: [apple-reminders]
prerequisites:
  commands: [things]
---

# Things 3 Skill

Add, update, list, search, or inspect Things 3 todos, inbox, today, projects, areas, and tags on macOS with the `things` CLI (things3-cli). Reads come from the local Things database; writes go through the Things URL scheme. This skill is macOS-only.

## When to Use

- User asks to add, complete, move, or check a Things todo
- Listing inbox, today, upcoming, projects, areas, or tags
- Searching Things for a todo by text

## Prerequisites

- **macOS only**, with Things 3 installed.
- Install `things` (recommended, Apple Silicon): `GOBIN=/opt/homebrew/bin go install github.com/ossianhempel/things3-cli/cmd/things@latest`
- If database reads fail, grant **Full Disk Access** to the calling app (Terminal for manual runs).
- Optional: set `THINGSDB` (or pass `--db`) to point at the `ThingsData-*` folder.
- Optional: set `THINGS_AUTH_TOKEN` to avoid passing `--auth-token` for update operations. Never print the token; pass it by environment or flag only.

## How to Run

1. Check `things` is on `PATH` with the `terminal` tool; if it is missing, install it (see Prerequisites).
2. Read with the database commands; write with `add` / `update`, previewing with `--dry-run` first.
3. Get the todo ID from `things search` before any update.

## Quick Reference

Read-only (local database):

```bash
things inbox --limit 50
things today
things upcoming
things search "query"
things projects
things areas
things tags
```

Write (URL scheme):

```bash
things --dry-run add "Title"
things add "Buy milk"
things add "Buy milk" --notes "2% + bananas" --when today --deadline 2026-01-02
things add "Book flights" --list "Travel"
things add "Call dentist" --tags "health,phone"
things --foreground add "Title"
```

From stdin, multi-line input becomes title plus notes:

```bash
cat <<'EOF' | things add -
Title line
Notes line 1
Notes line 2
EOF
```

## Procedure

1. Find the todo first for any change: `things search "milk" --limit 5` and take the ID from the UUID column.
2. Preview writes with `--dry-run`, which prints the URL and does not open Things.
3. Add with `things add "Title"` plus `--notes`, `--when`, `--deadline`, `--list`, `--heading`, `--tags`, or repeated `--checklist-item` flags as needed.
4. Update with `things update --id <UUID> --auth-token <TOKEN>`: new title positionally, `--notes` to replace, `--append-notes` / `--prepend-notes` to extend, `--list` / `--heading` to move, `--tags` to replace or `--add-tags` to add, `--completed` / `--canceled` to finish.
5. Report back the todo title, its list, and its new state.

### Deleting a todo

Not supported by `things3-cli` right now: there is no delete or move-to-trash write command (`things trash` is a read-only listing). Use the Things UI to delete or trash, or mark the todo `--completed` / `--canceled` with `things update`.

## Pitfalls

- macOS-only. Database reads need Full Disk Access for the calling app; without it reads fail even though Things itself works.
- Update operations need an auth token. Without `THINGS_AUTH_TOKEN` or `--auth-token`, updates fail.
- `--dry-run` prints the URL and does not open Things: use it before any write the user has not seen.
- Do not guess a todo ID. Search first; the UUID column is the ID.
- Never print the auth token. Refer to it by name only.

## Verification

- After an add, run `things search` for the title and confirm it appears in the expected list.
- After an update, re-run `things search` for the todo and confirm the title, notes, tags, or state changed as intended.
- After a complete or cancel, confirm the todo no longer appears in `things today` or `things inbox` as open.
