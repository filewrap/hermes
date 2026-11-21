---
title: "Create Template — Package this agent for another person to review and use"
sidebar_label: "Create Template"
description: "Package this agent for another person to review and use"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Create Template

Package this agent for another person to review and use.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/productivity/create-template` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `Template`, `Sharing`, `Export`, `Agent`, `Productivity` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Create a Template Skill

Turn this agent into a shareable Template for a person to review. Hermes
has no Control UI, so the skill mints a private draft under the active
profile home and bundles it into one shareable `.hermes-template` file
with the bundled script (skill-relative path
`scripts/template_export.py`). The exported file is the review artifact:
the person imports it under their own home and reviews the draft there.

## When to Use

- The user asks to share this agent as a template, package it for
  someone else, or turn it into something another person can use
- Only on the user's ask. Never start a template on your own initiative,
  and never package the agent because a conversation happens to look
  reusable.

## Prerequisites

- Run commands with the `terminal` tool; read files with `read_file`
- The `skills_list` tool for the installed skills, and `hermes plugins
  list` plus `hermes skills search <query>` via `terminal` for plugin
  inventory and ClawHub grounding
- `skill_manage` for creating or editing skills — the template skill
  never hand-writes a `SKILL.md` and never duplicates skill creation

## How to Run

1. Mint a draft for this request: `python3 scripts/template_export.py
   new --name <slug> --description "<what another person can do with
   it>"`. It prints a draft id and path under
   `<home>/template-drafts/<draft-id>.json`. Use that id and edit only
   that file; never look for a latest draft or use a shared pointer.
   Overlapping Template creations stay isolated by distinct ids.
2. Capture the reusable agent, inventory only relevant material, and
   author the proposal (see Procedure).
3. Check the draft with `python3 scripts/template_export.py validate
   <draft-id>`, then bundle it with `python3
   scripts/template_export.py export <draft-id>`. The export writes
   `<home>/template-drafts/<name>-<version>.hermes-template`.
4. Hand the exported file to the person and say what the Template does.
   Keep internal draft paths and ids out of your reply.

## Quick Reference

```bash
python3 scripts/template_export.py new --name research-brief --description "A collaborative research partner."
python3 scripts/template_export.py validate DRAFT_ID
python3 scripts/template_export.py export DRAFT_ID
python3 scripts/template_export.py export DRAFT_ID --out ~/research-brief-0.1.0.hermes-template
python3 scripts/template_export.py import ~/research-brief-0.1.0.hermes-template
hermes plugins list
hermes skills search research
```

`export` prints the shareable file path; `import` prints the new draft
id and path under the importer's home. A fresh draft fails validation
until its scope is complete — that is the gate working, not an error to
work around.

## Procedure

1. **Capture the reusable agent.** Read the current draft JSON. Use this
   conversation and the agent's durable instructions to identify its
   coherent reusable role, input, job, output, and authority. Preserve
   useful behavior and settings; leave out personal context. If the
   responsibilities do not form one coherent Template, ask which
   responsibility to share before writing. Complete when the four scope
   fields describe an agent a new person can use.
2. **Inventory only relevant material.** Select portable workspace
   instructions and files that implement that role. Run `skills_list`
   and `hermes plugins list` via `terminal`, and compare the results
   with tools actually available in this session and capabilities used
   in the role. Inspect a candidate plugin with `hermes plugins show
   <name>` for its source and exact version, then ground it with
   `hermes skills search <query>`. Declare a skill or plugin only when you can ground it in
   a current ClawHub package reference and exact version; plugin
   availability alone is not a requirement. Put package references in
   `packages`, MCP requirements in `mcpServers`, and useful schedules
   in `cronJobs`. Their declarations are dependencies, not files copied
   into the archive. Complete when every included file and declaration
   has a reason tied to the reusable role.
3. **Author the proposal.** Follow [the draft format](https://github.com/NousResearch/hermes-agent/blob/main/skills/productivity/create-template/references/draft-format.md).
   Rewrite instructions for a new owner. Include input, job, output,
   and authority in the draft `scope`, and describe the portable
   behavior in the `manifest`. Place only newly authored portable text
   in `files`, mapping included workspace files by `source` in the
   manifest. If the Template needs a skill that does not exist yet as a
   portable skill, create it with `skill_manage` first, then reference
   its portable content in the draft — never hand-write skill files
   into the draft. Exclude raw conversations, memories, `USER.md`,
   credentials, secret values, private URLs, local paths, and personal
   facts. Never copy the source workspace tree. Complete when the JSON
   at this turn's draft path is valid and contains no private source
   material.
4. **Hand off for review.** Briefly tell the person what the Template
   does, which optional files and dependencies you included, and any
   capability you could not port. Do not print internal draft paths or
   ids, generate a review link, upload, or publish. Send the exported
   `.hermes-template` file; the person imports it with
   `template_export.py import` and reviews the draft under their own
   home. Complete when the person has the file.

## Pitfalls

- A draft id belongs to one request. Minting a second draft for the
  same request, or editing another request's draft file, mixes two
  Templates into one.
- Availability is not a requirement. A skill or plugin that merely
  exists on this host does not belong in the Template unless the role
  uses it and it has a current ClawHub reference with an exact version.
- The export gate refuses credential-like content and private paths.
  Rewording a secret so the gate misses it is not a fix — remove the
  secret. Example placeholder values (`<your-key>`, `YOUR_API_KEY`)
  pass; real-looking values do not.
- The manifest body is the portable persona. Do not also map `SOUL.md`
  as a draft file.
- Do not promise publication or a review link. Hermes has no template
  catalog or review UI; the exported file is the whole hand-off.
- Never copy `MEMORY.md`, `USER.md`, or the source workspace tree into
  the draft, even when the role seems to need them — rewrite what is
  portable instead.

## Verification

- `validate` passes on the finished draft, and `export` writes the
  `.hermes-template` file next to it.
- The exported file re-imports cleanly: `import` on a fresh home
  prints a new draft id, and that draft validates.
- The draft contains no raw conversation, memory, credential, private
  URL, local path, or personal fact — every `files` entry traces to the
  reusable role.
- The person received one file and a short description of what the
  Template does.
