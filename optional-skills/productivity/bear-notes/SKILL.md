---
name: bear-notes
description: "Create, search, and manage Bear notes via grizzly CLI."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Bear, Notes, macOS, note-taking]
    category: productivity
    related_skills: [apple-notes, obsidian]
prerequisites:
  commands: [grizzly]
---

# Bear Notes Skill

Create, read, and manage notes in Bear on macOS with the `grizzly` CLI. This skill is macOS-only: Bear runs on Apple platforms, and `grizzly` drives it through Bear's URL scheme. It does not manage any other notes app.

## When to Use

- User asks to create, read, search, or append to a Bear note
- Listing Bear tags or opening notes by tag
- Anything that starts with "in Bear" or "my Bear note"

## Prerequisites

- **macOS only.** Bear app installed and running; Bear must be running for commands to work.
- Install `grizzly`: `go install github.com/tylerwince/grizzly/cmd/grizzly@latest`
- Some operations (add-text, tags, open-note with selection) need a Bear API token, stored in `~/.config/grizzly/token`. Get it from Bear: Help -> API Token -> Copy Token, then save it with `echo "YOUR_TOKEN" > ~/.config/grizzly/token`. Never print the token value; pass it by file only.

## How to Run

1. Check `grizzly` is on `PATH` with the `terminal` tool; if it is missing, install it (see Prerequisites) and stop there if the install fails.
2. Run the `grizzly` command for the task via `terminal`, preferring `--dry-run` or `--print-url` first for writes.
3. Use `--enable-callback --json` whenever data must come back from Bear.

## Quick Reference

```bash
echo "Note content here" | grizzly create --title "My Note" --tag work
grizzly create --title "Quick Note" --tag inbox < /dev/null
grizzly open-note --id "NOTE_ID" --enable-callback --json
echo "Additional content" | grizzly add-text --id "NOTE_ID" --mode append --token-file ~/.config/grizzly/token
grizzly tags --enable-callback --json --token-file ~/.config/grizzly/token
grizzly open-tag --name "work" --enable-callback --json
```

Common flags: `--dry-run` previews the URL without executing; `--print-url` shows the x-callback-url; `--enable-callback` waits for Bear's response (needed for reading data); `--json` outputs JSON when using callbacks; `--token-file PATH` points at the Bear API token file.

## Procedure

1. Confirm the task: create, read, append, list tags, or search by tag.
2. For a create, take the title and optional tag from the user; pipe the content on stdin or pass an empty stdin for a blank note.
3. For a read or search, always pass `--enable-callback --json` so Bear returns the data instead of just opening the app.
4. For an append, pass the note ID and `--mode append` with `--token-file ~/.config/grizzly/token`.
5. Report back the note ID, title, and tags for anything created, and the note content for anything read.

### Configuration

Grizzly reads config in priority order: CLI flags, environment variables (`GRIZZLY_TOKEN_FILE`, `GRIZZLY_CALLBACK_URL`, `GRIZZLY_TIMEOUT`), `.grizzly.toml` in the current directory, `~/.config/grizzly/config.toml`.

Example `~/.config/grizzly/config.toml`:

```toml
token_file = "~/.config/grizzly/token"
callback_url = "http://127.0.0.1:42123/success"
timeout = "5s"
```

## Pitfalls

- Bear must be running. If a command silently does nothing, check Bear is open before anything else.
- Note IDs are Bear's internal identifiers (visible in note info or via callbacks). Do not guess one.
- Use `--enable-callback` when data must come back; without it the command fires and returns nothing.
- Some operations require a valid token (add-text, tags, open-note with selection). A failure on those usually means the token file is missing or wrong, not that the note is missing.
- Never print the token. Refer to it by path only.

## Verification

- After a create, re-read the note by ID with `--enable-callback --json` and confirm the title and content match.
- After an append, re-read the note and confirm the new text is present.
- After a tag search, confirm the returned notes actually carry the tag.
