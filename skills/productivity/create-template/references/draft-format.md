# Private Template draft format

Write only the request-specific JSON draft at
`<home>/template-drafts/<draftId>.json`, using a draft id minted for this
request (`scripts/template_export.py new`). Never infer a target from
creation time or a shared pointer. Keep these fields:

```json
{
  "name": "portable-agent-slug",
  "description": "What another person can do with this agent.",
  "version": "0.1.0",
  "scope": {
    "input": "What a new user provides.",
    "job": "What the agent does.",
    "output": "What the user receives.",
    "authority": "What the agent may do without further approval."
  },
  "manifest": "---\nschemaVersion: 1\nagent:\n  id: portable-agent-slug\n  name: Portable Agent\n  description: What another person can do with this agent.\n---\nPortable instructions for a new owner.\n",
  "files": []
}
```

The manifest is the complete portable instruction text: YAML frontmatter
with `schemaVersion: 1` and an `agent:` block (`id`, `name`,
`description`), then the instructions themselves. The `name` is a
lowercase slug starting with a letter; `version` is exact semver. The
person can change the name and description when they review the draft.

For each portable workspace file, add its newly authored contents to
`files`, for example `{ "path": "workspace/AGENTS.md", "content": "..." }`,
and map it in manifest frontmatter:

```yaml
workspace:
  bootstrapFiles:
    AGENTS.md:
      source: workspace/AGENTS.md
  files:
    - source: workspace/reference/checklist.md
      path: reference/checklist.md
```

Use `workspace.bootstrapFiles` for supported startup files and
`workspace.files` for other managed files. A nonempty manifest body
already supplies `SOUL.md`; do not also map `SOUL.md` separately.
Include optional dependencies only when relevant:

```yaml
packages:
  - kind: skill
    source: clawhub
    ref: "@publisher/skill-name"
    version: 1.2.3
  - kind: plugin
    source: clawhub
    ref: "@publisher/plugin-name"
    version: 2.0.0
```

These package entries reference ClawHub versions. They do not copy skill
or plugin code into the Template. Put MCP requirements in `mcpServers`
and useful schedules in `cronJobs` in the same frontmatter. Keep tokens,
credentials, personal accounts, and runtime-specific model choices out of
the draft.

## Export: the shareable file

Hermes has no review UI, so the export is the review artifact.
`scripts/template_export.py export <draft-id>` validates the draft and
writes one file next to it:

`<home>/template-drafts/<name>-<version>.hermes-template`

The file is a JSON envelope holding the draft whole:

```json
{
  "format": "hermes-template",
  "formatVersion": 1,
  "exportedAt": "2026-11-11T12:00:00Z",
  "draft": { "...the draft document..." }
}
```

Send that one file to the person. They import it with
`scripts/template_export.py import <file>`, which validates the envelope
and writes a fresh draft under their own home for review. Import never
overwrites an existing draft: a colliding id is an error, mint another.

## What the export gate rejects

The export step is the review gate. It refuses the draft when:

- the name is not a lowercase slug, or the version is not exact semver;
- any of the four scope fields is blank;
- the manifest has no frontmatter, no `schemaVersion: 1`, no `agent:`
  block with an `id:`, or an empty body;
- a file path is absolute, escapes with `..`, or carries a private
  segment (`.env`, `.git`, `.openclaw`, `.hermes`, `memory`, `USER.md`,
  `sessions`, `transcripts`, `credentials`, `auth-profiles.json`,
  `secrets`), or ends in `.jsonl`;
- two files map to the same path (case-insensitive);
- any content — manifest, description, scope, or file — looks
  credential-like (a known token prefix, a credential assignment with a
  real-looking value, a PEM block). Example placeholder values
  (`<your-key>`, `YOUR_API_KEY`) pass; real-looking values do not.

Fix the draft and export again; never work around a rejection by
rewording a secret into a shape the gate misses.
