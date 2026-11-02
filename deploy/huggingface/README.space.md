---
title: Hermes Agent
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
startup_duration_timeout: 30m
---

# Hermes Agent on Hugging Face Spaces

This is an isolated Docker deployment for testing Hermes. It does not replace the production image. The app page is a small read-only status endpoint, not a chat UI, dashboard, terminal, or file server.

## Build inputs

Copy `Dockerfile.hf` to the Space root as `Dockerfile`, copy `Dockerfile.hf.dockerignore` as `Dockerfile.dockerignore`, and retain the `deploy/huggingface/` helpers. Use this file as the root `README.md`.

The reviewed application source pin is **`a0923910e33f78f3487311a202c8803934c879b7`**, which includes Mongo-primary storage and full-profile HF bucket backups; deployment helpers are copied independently from one clean reviewed private-fork checkout. Record that checkout's `git rev-parse HEAD` as the helper-bundle revision. Do not self-pin the commit being authored or substitute a moving `main` ref. Updating this source pin does not build or deploy an image.

Transcript auditing is strictly private-DM-only. Group, supergroup, channel, and forum messages are never enqueued, forwarded, or used to create transcript topics, regardless of sender/owner status, allowlists, triggers, or topic/thread; pending group-scope transcript rows must be dropped. Authorized private-DM transcripts remain eligible for the owner-configured private forum. Trusted startup/deployment notices in General and sanitized operational events in each profile's `Operations · Profile <canonical hash>` topic remain supported; these are not group transcripts. Multiple selected profiles may use one forum only through the live served registry and pinned profile identity, with each profile's durable outbox isolated and mismatches failing closed. Delivery is bounded best-effort, not exactly-once, and honors a shared forum FloodWait cooldown. The owner remains unrestricted and local tools are retained; existing cgroup/quota/network limitations are unchanged. Namespace tooling and the HF host's kernel permissions have not been validated here: tenant execution fails closed without a successful local probe and never falls back to host shell. This source-only change does not deploy a Space or restart a gateway/service. After a separately approved restart, run `/logs setup <negative_numeric_forum_id>` followed by `/logs status` in the owner's private chat for each selected routed profile; live verification is not complete here.

The image fetches this fork from `https://github.com/filewrap/hermes-agent.git` at the reviewed `HERMES_REF` commit. Change the Docker build arguments only after review (ordinary runtime variables do not override Docker ARG defaults). The ref must be a full lowercase 40-character commit SHA.

For private GitHub source, add a Space secret named `GITHUB_TOKEN` with read-only Contents permission for that repository. BuildKit mounts it for askpass authentication; it is not stored in the source URL or Git configuration. Space secrets can also be present at runtime, so keep this token narrowly scoped.

## Explicit trial startup

The default `HF_STATE_MODE=unconfigured` starts only the HTTP status service. To launch the gateway with disposable local state, set both:

```text
HF_STATE_MODE=ephemeral
HF_ALLOW_EPHEMERAL=1
```

`ephemeral` is a runtime mode value, not a secret or API key. These settings let the gateway start with live profile files in container-local `/home/Hermes/.hermes` and acknowledge that those files can be lost. They do **not** activate MongoDB or bucket storage. The supervisor still requires this acknowledgement even when backups are configured: HF chat SQLite stays local, and `/data` receives backup snapshots only, not a live SQLite database. Mongo requires a separately verified one-time migration; bucket snapshots require Hugging Face's automatic `SPACE_ID`, a valid `BUCKET_NAME`, and an actual writable bucket mount at `/data`.

`HF_STATE_MODE=local` is an alias and still requires the same acknowledgement. It does not make storage persistent. The default live state path is `/home/Hermes/.hermes`.

You do **not** need to create or mount `config.yaml` for a first trial. Leave `HF_CONFIG_FILE` and `HF_RESTORE_ARCHIVE` unset. On first startup, the helper creates `/home/Hermes/.hermes/config.yaml` privately (directory `0700`, file `0600`) with:

```yaml
model:
  default: openrouter/free
  provider: openrouter
  base_url: https://openrouter.ai/api/v1
  api_mode: chat_completions
fallback_providers: []
provider_routing:
  allow_fallbacks: false
  max_price: {prompt: 0, completion: 0, request: 0, image: 0}
auxiliary:
  free_only: true
  openrouter_model: openrouter/free
terminal:
  backend: local
```

Every available auxiliary task is also explicitly pinned to this OpenRouter route; delegation uses the same route with no fallback chain.

This selects only the [OpenRouter free router](https://openrouter.ai/docs/guides/routing/routers/free-router), not the paid auto router. It does not copy your existing bot setup or credentials. Set these in the Space's **Variables** and **Secrets**:

| Space Variables | Value / purpose |
| --- | --- |
| `HF_STATE_MODE` | `ephemeral` to enable the gateway with explicitly disposable local state. |
| `HF_ALLOW_EPHEMERAL` | `1`; acknowledges possible state loss, even when backups are configured. |
| `HERMES_HOME` | `/home/Hermes/.hermes` (the default live profile location; keep it off `/data`). |
| `OWNER_TELEGRAM_ID` | Your canonical positive decimal Telegram user ID; set as a Variable or Secret for owner-only controls. |
| `TELEGRAM_ALLOWED_USERS` | Recommended: comma-separated canonical positive decimal Telegram IDs allowed to use the bot. |
| `BUCKET_NAME` | Required bucket owner/name in `owner/name` form for mounted snapshots. |
| `MONGODB_DATABASE` | Optional target database; defaults to `hermes`. Use a unique name for migration tests. |
| `HF_PORT` | Optional; defaults to `7860`. If changed, also change Space `app_port`. |

| Space Secrets | Purpose |
| --- | --- |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN` | Telegram MTProto/bot credentials. |
| `OPENROUTER_API_KEY` | Needed for the default OpenRouter model route. |
| `MONGODB_URI` | Needed only when using a profile already migrated and activated for Mongo primary storage; adding the URI alone does not activate Mongo. |

Hugging Face automatically provides the canonical `SPACE_ID`; do not add it yourself. Set `BUCKET_NAME` to the owner/name of the bucket actually attached read/write at `/data`. Both chat and full-profile writes require valid `SPACE_ID` and `BUCKET_NAME`, an exact `/data` mount, and a successful write/read-back probe. `BUCKET_NAME` alone never enables writes. For build access to this private GitHub source, add `GITHUB_TOKEN` as a narrowly scoped, read-only **build secret** with repository Contents read permission. It is used only by BuildKit to fetch source. The optional `HF_TOKEN` is not needed for mounted bucket snapshots. Use a separate test bot while another deployment is running. Gateway process liveness does not prove that the chosen provider/model or Telegram credentials work.

Mongo activation is a separate one-time operation: migrate the exact owner profile while its gateway is stopped, verify the migration, and install its activation marker. `MONGODB_DATABASE` selects the target database (default `hermes`); isolated migration tests must use a unique database and a separate profile copy. A URI or a new HF Space does not perform this migration. Do not start a gateway on an un-migrated profile expecting Mongo to be primary.

### HF Telegram owner bridge and profile scope

The HF supervisor passes an internal, in-memory runtime environment payload to the gateway child process, pinned to the exact launch home. `gateway.mongo_multiuser._profile_env` overlays the payload's `OWNER_TELEGRAM_ID` and, only when explicitly supplied, `TELEGRAM_ALLOWED_USERS` for that launch home alone. The bridge never writes either setting to `.env` or any other file; an existing `.env` remains byte-for-byte unchanged, including permissions. A conflicting explicit owner or allowlist in the launch profile fails before gateway launch; values are compared rather than merged, so the bridge never combines allowlists or grants. Matching allowlists are compared as numeric sets (order and duplicates are immaterial). Owner and allowlist IDs must be canonical positive ASCII decimal numbers, with no leading zeroes or wildcards. Without an owner, there is no bridge overlay. Raw HF variables without the supervisor's internal runtime marker do not affect ordinary local behavior.

The internal payload is not logged, written to config, or included in snapshots. Other and routed profiles do not inherit the launch owner's identity or allowlist. Existing tenant access-state and ban persistence is unchanged; numeric Telegram identities may occur in tenant state needed for access or history, but bridge bootstrap settings are not persisted. Startup does not reset durable bans or revocations; the owner can admit a numeric ID with `/allow`. `MULTIUSER_ENABLED` is retired. Validation should assert no bridge bootstrap settings reach disk (including `.env`), logs, config, or snapshots; conflicts fail before launch without merging grants; and A-B-A isolation (launch A, run B, return to A) prevents profile inheritance. Also exercise absent owner, owner-only and owner-plus-allowlist payloads, canonical-ID checks, and unchanged `.env` bytes and permissions. This describes the startup contract; it does not claim a live Space was changed or tested.

For custom non-secret settings, `HF_CONFIG_FILE` optionally selects an existing absolute mounted seed-config path. It is a path **inside the container**, not a file created by setting the variable. Existing/restored configuration is never overwritten; missing configuration is generated only when no seed file was requested. An explicitly invalid seed path or an existing empty config still fails rather than silently discarding your choice. Existing/restored configs that select another model/provider, enable fallback chains, or weaken the free-only policy are preserved but **block gateway startup**; update their routing settings explicitly rather than expecting silent migration. Consult `README_HF.md` for a complete custom-config example, dataset mounting instructions, and the Telethon/MTProto requirements.

Optional `HF_RESTORE_ARCHIVE` selects a trusted immutable `.tar.gz` created by the snapshot helper. It takes precedence over automatic bucket restore. Restore happens only into absent/empty local state before the gateway starts. Nonempty destinations are never merged or overwritten.

### Full-profile secondary bucket snapshots

When Hugging Face's platform-provided `SPACE_ID` is present, `BUCKET_NAME` names the owner/bucket path, a private bucket is attached read/write at `/data`, `/data` is an exact mountpoint, and a write/read-back probe succeeds, the supervisor creates a full-profile backup under `/data/hermes-full-state/{owner}/{bucket}/` when durable content changes and after graceful shutdown. It includes credentials and `.env`, chat/message history, SQLite databases (including Telethon `.session` databases), and other durable profile files. SQLite is copied through the backup API and integrity-checked; archive checksums are read back before a commit marker is published. Identical snapshots are deduplicated and only the three newest validated snapshots are retained. Startup recovery restores absent files only and never overwrites existing local files. A missing/invalid `BUCKET_NAME`, a plain `/data` directory, or an unmounted VPS path cannot enable bucket reads or writes.

The full archive deliberately contains plaintext secrets and transcripts. Keep the mounted bucket private and access-limited; Hermes does not encrypt these archives. Mongo, when separately activated after a verified profile migration, remains primary for non-chat state, while the bucket is secondary for the complete profile. Neither backup path starts or restarts a service.

## Required runtime safety gates

The launcher forces `HERMES_STRICT_FREE_ONLY=1`; a Space variable set to `0` cannot turn it off. The pinned application source must contain the versioned runtime guard or startup fails. Config checks are not the only protection: built-in main and auxiliary request paths must reject alternate providers/models/transports and prevent request-body routing overrides. The policy admits only `openrouter/free` at `https://openrouter.ai/api/v1`, sets all supported price caps to zero, and disables fallback. Unavailable free capacity must produce a failure, never a paid retry. Presets, server-side tools, plugins, unknown extra-body controls, and separately processed file/audio inputs are rejected; local function tools and text/image inputs remain eligible. Restored dotenv files and secret-source refreshes cannot disable a launcher-enforced strict flag. This is an application routing policy, not an OS sandbox against arbitrary code or a terminal command using credentials.

The image builds SQLite 3.53.4 and forces `uv` to use `/usr/bin/python3` without downloading a Python with bundled SQLite. Both the **actual `.venv/bin/python` at image build** and the supervisor interpreter at every gateway startup must pass SQLite >=3.51.3 and an FTS5 trigram smoke test. SQLite 3.50.4 now blocks startup with a clear message rather than relying on DELETE-journal fallback. The launcher will not start the gateway if a runtime safety gate fails.

The frozen source lock installs **uvloop 0.22.1** on the image's supported Linux CPython runtime. At image build and before each configured gateway launch, `check_runtime.py` also runs a state-free coroutine through the actual `gateway.event_loop.run_gateway_async` selector and requires `uvloop.Loop` with normal teardown. Missing/wrong uvloop or unexpected asyncio fallback blocks startup before state setup. The HTTP front door still binds first, and unconfigured mode still launches no gateway. Do not install a process-wide uvloop policy or replace the gateway selector with standard `asyncio.run()`. Other unsupported application platforms retain their normal fallback.

The pinned source preserves the five-way Telethon/MTProto downloader, 512-KiB chunks, completion-order refill and offset writes, decimal MB/MB/s output, 10-second diagnostics, and removal of `download_limits`. It does not claim a measured HF throughput improvement.

## Sync and rebuild (operator action)

In a local clone of this fork checked out at the reviewed deployment commit, set `SOURCE_REPO` and `SPACE_REPO` to the two absolute clone paths. These commands change only the Space repository; run them only when ready to rebuild:

```sh
SOURCE_REPO=/absolute/path/to/hermes-agent
SPACE_REPO=/absolute/path/to/space-repo
mkdir -p "$SPACE_REPO/deploy/huggingface"
cp "$SOURCE_REPO/Dockerfile.hf" "$SPACE_REPO/Dockerfile"
cp "$SOURCE_REPO/Dockerfile.hf.dockerignore" "$SPACE_REPO/Dockerfile.dockerignore"
cp "$SOURCE_REPO"/deploy/huggingface/*.py "$SPACE_REPO/deploy/huggingface/"
cp "$SOURCE_REPO/deploy/huggingface/start.sh" "$SPACE_REPO/deploy/huggingface/"
cp "$SOURCE_REPO/deploy/huggingface/README.space.md" "$SPACE_REPO/deploy/huggingface/"
cp "$SOURCE_REPO/deploy/huggingface/README.space.md" "$SPACE_REPO/README.md"
git -C "$SPACE_REPO" add Dockerfile Dockerfile.dockerignore README.md deploy/huggingface
git -C "$SPACE_REPO" diff --cached --stat
git -C "$SPACE_REPO" commit -m "Sync reviewed uvloop runtime and HF helper checks"
git -C "$SPACE_REPO" push
```

Keep `HF_STATE_MODE=ephemeral`, `HF_ALLOW_EPHEMERAL=1`, and credentials in Space secrets. Leave `HF_CONFIG_FILE` and `HF_RESTORE_ARCHIVE` unset for a fresh bootstrap. Do not set a stale custom `HERMES_REF` build override. A push triggers a new build; if needed use the Space's **Factory reboot** action to rebuild without cached layers. Container-local state can be lost: export anything needed before rebuilding. Never expose snapshots containing credentials in the repository.

Before sending a model prompt, confirm the new build uses the reviewed source pin, the actual-venv SQLite and uvloop/gateway-loop verification passes, and runtime logs show no policy/startup failure. `/readyz` proves process liveness only. This change does not itself rebuild the Space, validate its credentials, or send a model prompt.

## Status endpoints

- `/`: safe deployment status.
- `/healthz`: HTTP-service liveness; can be healthy while the gateway is intentionally unconfigured.
- `/readyz`: gateway subprocess liveness only; does not prove database, model or messaging health.

Unexpected gateway exit or state setup failure terminates the deployment with an error rather than leaving a dummy healthy server running forever.

## Persistence and security

Dataset mounts are read-only. Writable HF bucket mounts are suitable for closed snapshot artifacts and exports, but are not treated as proven-safe live SQLite storage. `/data` is writable by user `Hermes` in the image; a runtime mount has its own permissions. Do not place the live home or live state subdirectories on a bucket or dataset mount.

Container-local changes can be lost on restart. No continuous backup job is installed. Snapshot restore recovers only the chosen archived baseline, not changes made since it. The helper uses SQLite backups and verified non-destructive restore; all writers must be quiesced for a coherent whole-home snapshot.

The user `Hermes` has UID/GID 1000 and passwordless sudo inside this container. Keep the Space and mounted bucket private: full-profile snapshots contain plaintext credentials and chat transcripts and are not encrypted by Hermes. Use a test bot and scoped credentials, and explicitly restrict allowed messaging users. Do not expose an agent terminal or administrative service without a separately reviewed authentication design.
