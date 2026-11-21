---
title: MongoDB-primary storage and backups
---
# MongoDB-primary storage and backups

Hermes keeps local files and SQLite as the default until a complete owner-data migration has been verified. A Mongo URI by itself never changes the active backend. After explicit activation, MongoDB is primary for non-chat state; local SQLite remains the message-history store and cache. Hermes does not silently fall back to local writes when an activated Mongo backend is unavailable.

The migration preserves all local sources and does not restart the gateway. It includes provider and account credentials, profile settings, instructions, memory, custom skills, tenant/access data, accounting and non-chat SQL state. It excludes the `messages` table and its full-text indexes, plus tenant transcript-history documents. Durable support tables such as system prompts, model usage, routing, delivery obligations and async-delegation state are retained; only live heartbeats and lock/lease rows are omitted. Session metadata is retained, but message bodies are not copied to Mongo.

## Review and migrate a profile

First run a read-only plan. It reports counts and never prints the Mongo URI or credential payloads:

```bash
python -m agent.mongo_migration --home /path/to/profile
```

When the plan is correct, stop the gateway and run the idempotent migration with explicit activation:
The Mongo destination is selected by `MONGODB_DATABASE` (defaults to `hermes` when unset). For an isolated test, use a unique database name and an isolated copy of the profile, such as `MONGODB_DATABASE=hermes_migration_test_20261025`; do not point test commands at the live `hermes` database. When `--home` names a copied profile, place the test URI and database name in that copy's `.env` so bootstrap reads the isolated settings rather than the source profile.

```bash
python -m agent.mongo_migration --home /path/to/profile --apply --enable-primary --gateway-stopped
```

The command applies only the exact reviewed profile plan, verifies remote records and SQL snapshots, and keeps local source files unchanged. `--gateway-stopped` is an operator confirmation; the command never stops or restarts a service. The new primary marker is read on the next process start. If migration reports a conflict or incomplete verification, Mongo is not activated; do not delete either local data or Mongo records to force it through.

Mongo primary mode requires the same owner identity and URI that were used for migration. If the activation marker, owner or URI does not match, Hermes fails closed rather than switching back to local storage. Mongo transport requires verified TLS. `MONGODB_DATABASE` explicitly selects the target database; it defaults to `hermes` only when unset.

## Hugging Face bucket backups

In a Hugging Face Space, the supervisor also creates complete, compressed profile snapshots under `/data/hermes-full-state/{owner}/{bucket}/` and chat snapshots under `/data/hermes-state/{owner}/{bucket}/` when all checks pass: the platform-provided `SPACE_ID` and configured `BUCKET_NAME` (`owner/name`) are valid, `/data` is an exact mountpoint, and a write/read-back probe succeeds. A bucket-name environment variable alone cannot enable this path. A VPS or other machine with ordinary, unmounted `/data` keeps bucket writes disabled.

These full-profile snapshots include credentials, `.env`, provider settings, local message history, SQLite databases and Telethon `.session` databases. SQLite files are copied through SQLite's backup API and integrity-checked. Snapshots are content-deduplicated, checksummed, read back before publication, and limited to the three newest validated snapshots. Recovery restores only absent files; existing local files are never overwritten. An explicit `HF_RESTORE_ARCHIVE` takes precedence over automatic bucket recovery.

This is intentionally a separate secondary backup path. Mongo remains primary for migrated non-chat state; HF buckets hold the full profile, including transcripts and credentials. Snapshots are not encrypted by Hermes: use a private bucket with appropriate access controls. See the [Hugging Face storage-bucket documentation](https://huggingface.co/docs/hub/en/storage-buckets-access).

## Other backup and recovery tools

The optional Mongo snapshot worker (`python -m agent.mongo_backup --home /path/to/profile --once`) remains a separate operator-invoked backup/export facility; it does not change serving mode. It stores content-addressed backup chunks and manifests and does not copy chat-message rows. Recovery/export remains explicit and non-destructive; it does not merge with or overwrite an existing profile.

The owner `/sync` command reports sync status; it does not itself migrate, restore, or activate Mongo. Validate a migration with the CLI receipt, and review the profile's backup availability before changing deployments.

## Privacy

MongoDB and HF snapshots can contain plaintext credentials. TLS protects Mongo transport, not stored documents, and Hermes does not encrypt bucket archives. Restrict Mongo database permissions and bucket access, and enable the storage provider's encryption-at-rest controls where available. Never upload snapshots or live profile files to the source repository.
