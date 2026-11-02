# Hermes on Hugging Face Docker Spaces

A separate, opt-in deployment path for trying this fork on **Hugging Face Docker Spaces**, without replacing the existing Docker image, entrypoint, or production runtime.

**Read this first:** this deployment makes the Space's HTTP front door available promptly. That is not proof that the messaging gateway, Telegram connection, model provider, or storage recovery is working. Check `/readyz`, the runtime logs, and a real authorized bot conversation separately.

**Live state stays local; a verified mounted bucket holds recovery snapshots.** SQLite and its WAL remain under `/home/Hermes/.hermes`, never on `/data`. With Hugging Face's platform `SPACE_ID`, an exact writable `/data` bucket mount, and a successful read-back probe, the supervisor stores full-profile snapshots that include credentials, `.env`, and chat history, plus a separate allowlisted chat-continuation archive. These archives are not encrypted by Hermes. Without usable mounted storage, live state remains ephemeral and the deployment reports that explicitly. Snapshots reduce loss; they do not promise zero-loss recovery or immediate remote durability.

## Contents

1. [What is included](#what-is-included)
2. [Architecture and directory layout](#architecture-and-directory-layout)
3. [Create a private Docker Space](#create-a-private-docker-space)
4. [Build settings and private GitHub source](#build-settings-and-private-github-source)
5. [Runtime settings and first gateway launch](#runtime-settings-and-first-gateway-launch)
6. [Private dataset repositories](#private-dataset-repositories)
7. [Attach mounted storage](#attach-mounted-storage)
8. [Snapshots, upload, restore, and recovery](#snapshots-upload-restore-and-recovery)
9. [Ports, networking, and security](#ports-networking-and-security)
10. [Local image checks](#local-image-checks)
11. [Troubleshooting](#troubleshooting)
12. [Update and rollback checklist](#update-and-rollback-checklist)
13. [What has and has not been validated](#what-has-and-has-not-been-validated)

## What is included

| File | Purpose |
| --- | --- |
| `Dockerfile.hf` | Standalone HF-oriented image; it does not inherit the existing production entrypoint. |
| `Dockerfile.hf.dockerignore` | HF-specific build-context exclusions. |
| `deploy/huggingface/start.sh` | The HF-specific startup entrypoint. |
| `deploy/huggingface/supervisor.py` | A small HTTP front door plus supervised, opt-in gateway process. |
| `deploy/huggingface/persistence.py` | Separate allowlisted chat-continuation snapshots and local startup recovery. |
| `deploy/huggingface/full_state.py` | Full-profile bucket snapshots, including credentials and chat, when a real writable Space bucket is mounted at `/data`. |
| `deploy/huggingface/snapshot.py` | Offline/quiesced snapshot creation and non-destructive restore into a new home. |
| `deploy/huggingface/README.space.md` | Space root `README.md` template containing required Docker SDK metadata. |
| `README_HF.md` | This guide; **not** the Space's SDK configuration by itself. |

The image provides Linux system packages, `uv`, the locked Python dependencies, a real source checkout outside `/data`, and the explicitly requested user **`Hermes`** with UID/GID 1000 and passwordless sudo inside the container. `/home/Hermes` and the image's `/data` directory belong to that user.

The capital `H` matters on Linux. Existing production wrappers refer to different paths and a different user; do not reuse those wrappers in this image.

## Architecture and directory layout

```text
HF HTTPS reverse proxy
        |
        v
0.0.0.0:7860 — deployment HTTP front door
        |-- /          safe deployment status only
        |-- /healthz   front-door/process liveness
        |-- /readyz    gateway subprocess state, not end-to-end bot health
        |
        +-- opt-in Hermes gateway subprocess
                 |-- outbound model API requests
                 +-- outbound messaging connections, including Telegram MTProto

/home/Hermes/
    hermes-agent/       cloned application source and application .venv
    hf/                HF deployment helpers copied from the Space build context
    .hermes/           default LIVE state; local and ephemeral on HF
    hf-tools/          optional separate HF-management environment
/data/                 writable bucket mount, if attached at runtime
/mnt/hermes-seed/      optional PRIVATE DATASET mount; read-only
```

The exact helper directory is defined by `Dockerfile.hf`; use its startup command as the source of truth when extending the image.

### Why the application is not cloned into `/data`

An HF mount is attached **at runtime**, after the image is built. Mounting a volume over `/data` hides any files the build put there. Keep source code, the virtual environment, and startup scripts outside the mounted path so attaching storage cannot hide the executable application.

Creating and chowning `/data` during the build prepares the image's own directory. It **does not** assign ownership to a later HF volume or populate that volume. Runtime mount permissions still need to be verified.

### Conversation recovery versus a complete home backup

When bucket checks pass, `full_state.py` creates full-profile archives under `/data/hermes-full-state/{owner}/{bucket}/`, where `BUCKET_NAME` supplies the bucket owner/name. A valid `SPACE_ID`, valid `BUCKET_NAME`, exact writable `/data` mount, and successful write/read-back probe are all required. These include durable profile files such as settings, `.env`, provider and Telegram credentials/session databases, SQLite stores, and chat/message history. Transient logs, caches, virtual environments, and other explicitly excluded build/runtime artifacts are omitted. SQLite files are copied through the backup API and integrity-checked. Full-profile archives are not encrypted by Hermes and may contain plaintext secrets and private transcript text; keep the Space and bucket private and access-limited.

The separate `persistence.py` store writes a narrower, allowlisted chat-continuation archive under `/data/hermes-state/{owner}/{bucket}/`, using the same `BUCKET_NAME`, `SPACE_ID`, exact-mount, and write-probe gate. It retains approved session/message tables, sanitized routing entries, conversation-generation counters, and validated tenant access/history records; it excludes credentials, `.env`, Telegram authentication sessions, and arbitrary profile settings. This narrower archive complements but does not replace the full-profile backup. The manual `snapshot.py` helper has its own filtering rules and does not control either automatic archive format.

## Create a private Docker Space

These steps are for you to perform when ready. Merely committing these files to GitHub does not create or deploy a Space.

1. Open [Create a Space](https://huggingface.co/new-space).
2. Choose your account or organization and a new name, for example `hermes-test`.
3. Select **Docker** as the SDK and the blank Docker template if offered.
4. Start with a **private** Space. The image has an AI terminal-capable application and passwordless sudo; there is no reason to expose administrative surfaces during an initial test.
5. Choose suitable CPU hardware for your trial. Remote model inference does not require a local GPU. This does not guarantee every optional tool will fit your selected memory/disk budget.
6. Add the build files described below to the Space repository.

### Required root filenames

HF expects a root `Dockerfile` and a root `README.md` with YAML front matter. It does not automatically select a file named `Dockerfile.hf`, and it does not use `README_HF.md` as Space metadata.

In the **separate Space repository**, copy:

```text
GitHub source                          Space repository
Dockerfile.hf                      -> Dockerfile
Dockerfile.hf.dockerignore         -> Dockerfile.dockerignore
deploy/huggingface/                -> deploy/huggingface/
deploy/huggingface/README.space.md -> README.md
README_HF.md                      -> README_HF.md   (optional guide copy)
```

Keep the original production `Dockerfile` unchanged in this GitHub repository. Renaming/copying above applies only to the separate Space repository.

A minimal root Space card looks like:

```yaml
---
title: Hermes Agent Test
sdk: docker
app_port: 7860
startup_duration_timeout: 30m
---

# Hermes Agent Test

Private Hermes gateway deployment. The app page reports deployment status;
it is not an administrative console or chat UI.
```

`startup_duration_timeout` is a maximum startup allowance, not a request to wait 30 minutes. `suggested_storage` no longer provisions persistent storage; use attached volumes instead. The official [Docker Spaces guide](https://huggingface.co/docs/hub/en/spaces-sdks-docker) and [configuration reference](https://huggingface.co/docs/hub/en/spaces-config-reference) explain these requirements.

### Publish from a clean local staging directory

Use a clean checkout of the desired GitHub commit, **not** a live Hermes home. Do not upload your `.env`, SQLite files, Telethon sessions, caches, or credentials to the Space's Git history.

```bash
# Run from your clean hermes-agent source checkout.
SPACE_DIR="$(mktemp -d)"
cp Dockerfile.hf "$SPACE_DIR/Dockerfile"
cp Dockerfile.hf.dockerignore "$SPACE_DIR/Dockerfile.dockerignore"
mkdir -p "$SPACE_DIR/deploy"
cp -a deploy/huggingface "$SPACE_DIR/deploy/huggingface"
cp deploy/huggingface/README.space.md "$SPACE_DIR/README.md"
cp README_HF.md "$SPACE_DIR/README_HF.md"
printf 'Review this directory before uploading: %s\n' "$SPACE_DIR"
find "$SPACE_DIR" -maxdepth 4 -type f
```

Upload only that reviewed directory through the HF UI or a separately authenticated HF CLI. For example, with a current HF-management CLI installed:

```bash
hf auth login
hf upload YOUR_HF_NAME/hermes-test "$SPACE_DIR" . --repo-type space
```

This command really updates the Space and triggers a build; it is not a dry run. Substitute your actual namespace. Do not paste tokens into a command line or store them in the Space card.

## Build settings and private GitHub source

### Clone, then install, then copy deployment helpers

`Dockerfile.hf` clones the selected source revision into the Hermes-owned application directory outside `/data`. It installs Python packages with `uv` against the repository lockfile. It then copies the HF-specific helpers from the Space build context with `COPY --chown=Hermes:Hermes`.

The default source is this fork, `https://github.com/filewrap/hermes-agent.git`, not the upstream repository. The application source revision is pinned independently from the deployment helper files. After changing either one, review both before rebuilding.

### Current reviewed application and helper pair

`Dockerfile.hf` pins application source **`a0923910e33f78f3487311a202c8803934c879b7`**, the reviewed current `main` commit, from the private `filewrap/hermes-agent` fork. It includes the Mongo-primary storage and migration support plus full-profile HF bucket snapshots. Its `pyproject.toml` and `uv.lock` pin **uvloop 0.22.1** for supported Linux CPython (the HF image uses Debian system Python 3.13). `uv sync --frozen` installs from that source lock, not from a moving `main` branch or the Space's copied helper directory. This source-only pin alignment does not build or deploy an HF image.

This source retains the current five-way Telethon/MTProto downloader: five bounded connections/request slots, 512-KiB chunks, completion-order refill with offset writes, decimal MB/MB/s output, 10-second diagnostics, and removal of `download_limits`. Five-way concurrency is the existing owner-requested trial, not a claim of improved HF throughput. Provider credentials, image storage behavior and dependency pins are unchanged by this packaging update.

The application adds owner-private `/allowlist` policy controls, three bounded tenant adapters, profile/user/chat/topic isolation, fail-closed reply anchors and opt-in private forum auditing. Transcript auditing is strictly private-DM-only: group, supergroup, channel, and forum messages are never enqueued, forwarded, or used to create transcript topics, regardless of sender/owner status, allowlists, triggers, or topic/thread; pending group-scope transcript rows must be dropped. Authorized private-DM transcripts remain eligible. Trusted startup/deployment notices in General and sanitized operational events in each profile's `Operations · Profile <canonical hash>` topic remain supported and are not group transcripts. One adapter can route several selected profiles to the same private forum only through the live served registry and pinned identity, failing closed on unavailable or mismatched identity; profile outboxes remain isolated. The outbox is bounded best-effort, not exactly-once, and honors a shared forum FloodWait cooldown. The owner remains unrestricted and owner/local tools are retained; group guests gain no global access or tools. See [the DM-only audit policy, setup procedure and local boundary](plugins/platforms/telegram/TENANT_POLICY.md). The Bubblewrap/kernel namespace proof applies only to the tested local Linux host; it is not an HF image or host guarantee. An unavailable namespace leaves tenant execution explicitly unavailable, with no ordinary-host fallback. Existing cgroup/quota/network limitations are unchanged; aggregate tenant resource quotas are not provided.

The application SHA and the deployment-helper revision are deliberately independent. Copy the Dockerfile and **all** helpers together from one clean reviewed private-fork checkout; record `git rev-parse HEAD` as that helper-bundle revision before staging the Space files. The fetched application does not need to contain the current helpers: the image runs the separately copied `/home/Hermes/hf/` files. Never set `HERMES_REF` to the commit being authored merely to self-pin these helpers; that would require an impossible self-reference. This Dockerfile pins the already-completed application commit `a0923910e33f78f3487311a202c8803934c879b7`.

At image build and before each configured gateway launch, the actual application interpreter must pass the existing SQLite/FTS5 checks **and** import uvloop 0.22.1 and run a state-free coroutine through `gateway.event_loop.run_gateway_async`. That probe must observe `uvloop.Loop` and normal Runner teardown. The same gateway-only selector is used by `hermes_cli.main gateway run`; HF does not install a process-wide event-loop policy or wrap the gateway in standard `asyncio.run()`. The application's asyncio fallback remains available on unsupported non-HF runtimes, but an unexpected fallback in this supported Linux image blocks startup before state setup. The status front door still binds first; unconfigured mode still starts no gateway, `/healthz` remains HTTP liveness, and `/readyz` remains subprocess liveness only—not proof of model, Telegram, database or downloader health.

This is a source-only documentation/implementation change, not a Space deployment, image rebuild on HF, or gateway/service restart. No live `/logs setup` or `/logs status` verification is complete. After a separately approved restart, run `/logs setup <negative_numeric_forum_id>` and then `/logs status` in the owner's private chat for each selected routed profile. Historical validation below predates these changes and must not be read as acceptance testing of the updated implementation.

The current runtime-setup repair passed **1,029 complete Telegram platform tests**, including **74 audit regressions** (25 new raw-ID, bot compatibility, receipt-association and recovery cases), plus **149 tenant/access-security tests**. The HF helper/source-pin, strict-free-policy/error-handling and gateway event-loop suites passed **162 tests** using the existing `venv` with pinned uvloop 0.22.1; no dependency changes were made. Setup accepts raw positive Channel IDs only after live matching private-forum and bot-admin checks, stores canonical negative IDs, and verifies General/Operations/user topics by ID without bot-unsupported enumeration. Stable random IDs and durable exact-topic receipts prevent replacement creation on verification/notice retries. A previously recorded profile/routing run had **64 passed and three baseline-reproduced failures** (two connect-signature cases and one order-dependent completion-preflight case); that separate suite was not rerun for this bounded repair. Live forum rights and delivery remain unverified until owner setup. No HF deployment, live configuration edit or gateway/service restart is performed by this source-only change.

Previous uvloop/downloader packaging validation (superseded application pin `8e50e096408fdb7ab42ffc259e851732765bad8b`): **228 passed, zero failures** across the HF helper/strict-free-policy suites, gateway uvloop entrypoint/fallback tests, Telegram HF diagnostics, and bounded downloader/progress/UI regressions. That historical run also checked SQLite/FTS5, lock consistency, helper compilation and build-shell syntax; it is not validation of the new tenant policy on an HF host.

### Historical UI/source validation (superseded application pin)

The earlier UI packaging revision pinned application source commit `ddaee60b4c4652e2b384741d4a3b73bf57973195`, retaining the runtime-only, launch-profile-scoped HF owner bridge, reviewed conversation recovery, native Telegram diagnostics, context-aware reactions, and existing access/model/provider fixes. Personal settings, File downloads, and Skills now use a heading and scoped description, dividers, a complete values table, and a final italic `Page n of N • Total T` footer. Optional text reactions and the private skill library/auto-load selection are included in the settings table; all existing controls remain, with Close last. Skills retains the exact new-session warning and an actor-bound Back action to Personal settings. Help section-choice buttons omit only their leading ordinal prefixes (for example, `1. Session · 34` becomes `Session · 34`); category names, trailing counts, ordering, callback indexes, page navigation, and the final italic footer remain unchanged. Indexes and counts on unrelated screens are unchanged. Provider/model cards no longer duplicate the visible selection buttons in an extra page-item list. Current/selected provider buttons remain distinct; `:free` model IDs receive green highlighting with an explanatory hint, without changing paid/free policy or routing.

Standalone `/models` uses Close instead of Back. Close on the affected settings, downloads, skills, provider/model catalogs, and `/model` dashboard deletes only the bound menu and its explicitly captured invoking command. Unknown command IDs are never inferred from a task/topic reply anchor; entering from another menu cannot inherit an unrelated old invocation. Tracking remains feature-local, and existing DM/owner restrictions, 10-minute model-parameter prompts, and accepted-reply deletion remain in place. A requested rewrite must meaningfully change the prose; adding a button, Back glyph, or marker must preserve existing labels, counts, and actions. These UI editing rules and the italic final footer are regression-tested.

The help-label update was validated with `HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/plugins/platforms/telegram/test_help_ui.py tests/plugins/platforms/telegram/test_telethon_adapter.py -k help --tb=short` (**32 passed, zero failures**) and the same two modules without `-k help` (**74 passed, zero failures**). The new regression failed on the previous numbered labels before the implementation changed, and now verifies unnumbered category names with unchanged totals (including `Session · 34`), order, and callbacks across help views. No deployment, HF rebuild, or gateway restart was performed for this update. The broader validation below belongs to the previous settings revision and was not rerun for this help-only change.

That historical build did not automatically follow `main`; its matching tested HF helper bundle was commit `6d3802bfac0231a221e7bf5f929538977938b719` and is copied separately from the Space repository. The unchanged starting revision `d129a60` passed all 495 Telegram platform tests. Final validation passed **693 tests with zero failures across 41 files**, covering the complete Telegram platform suite, bounded model/settings/skills gateway integrations, and all 132 strict free-policy/HF helper tests. No baseline failures occurred in these selected suites; this is not a claim that the entire repository suite was run. The existing [offline model and Sampling preview](docs/model-ui-preview.html) predates these card changes and is not a preview of this revision. Logs showed no fresh `/settings` UI traceback; the separate OpenRouter expired-key error was left unchanged. No credentials, provider authentication, routing policy, global rich transport/renderers, deployed Space, or running gateway were changed. No Telegram demo, alternate client, deployment, HF rebuild, or local gateway restart was performed. These results are not deployed-Space validation.

### Tested /start welcome and access feedback

Source commit `31cd697f200452a26af45acb3ebb62e435751e09` updates `/start` to greet the sender by their full Telegram display name, introduce Naina as an AI assistant, briefly describe questions/research/writing/coding/task help, and include a concise `/help` hint. Names are literal native `TextPlain`/`TextBold` content, so markup in a name cannot become formatting. The welcome has no table, privacy section, or group-controls section.

Unlisted users receive the same welcome plus a gentle not-yet-added line and an initially collapsed native MTProto `PageBlockDetails` titled **How to get access**, linking to `@AgainOwner` to politely ask to be added. No HTML `<details>` is used. Unauthorized messages now receive a `🥴` reaction instead of a standalone pending-approval reply; unlisted `/start` also retains its tailored welcome and existing owner notification. Unsupported reactions fail safely without a text fallback. Starting still only records identity/start count: it does not grant access or enable triggers. Existing banned/revoked handling, `/allow` and `/allowall` policy, provider credentials, and BotCommands are unchanged.

Validation: **556 passed**, covering the complete Telegram platform suite plus local/Mongo tenant tests, with one dependency deprecation warning and zero failures. Tests cover literal full-name greetings, shared identity/capabilities/help, allowed/unlisted variants, serialized native collapsed blocks, unauthorized reactions and reaction failures, no access grant, and banned/revoked users. The HF persistence, snapshot, and supervisor helper suites also passed **73 tests** after the source-pin update. This is offline mocked-transport validation, not a live Telegram rendering test. No HF deployment/rebuild or local gateway restart was performed.

#### /start delivery repair

Tested source `acdf2ead0073c1dc9d9cff42f670272a8de7bc08` supplies a non-empty literal companion `message` alongside `rich_message`, matching the established MTProto sender, and uses `PageBlockHeading1` rather than the legacy Instant View `PageBlockHeader`. The old `/start` sender supplied an empty message and suppressed the server's RPC reason. The inspected agent, gateway, error, and service logs retain only `BadRequestError`, so the historical concrete RPC reason cannot be recovered; `MESSAGE_EMPTY` is a hypothesis, not a captured error. Native `PageBlockDetails(open=False)` is retained rather than blaming or removing the access disclosure.

On a definite rich-message `BadRequestError`, `/start` retries through ordinary MTProto text with `MessageEntityBlockquote(collapsed=True)` for the access instructions. Offsets and lengths count UTF-16 code units, including when names contain emoji. If that entity is rejected too, one final plain-text attempt keeps the entire welcome and access instructions. Names are always literal text. Timeouts and other ambiguous failures do not trigger a new fallback message. Diagnostics record bounded RPC symbols such as `MESSAGE_EMPTY` or `ENTITY_BOUNDS_INVALID`, never exception/request reprs, names, IDs, or tokens. Authorization, banned/revoked handling, `/allow`, `/allowall`, owner notifications, and the `🥴` reaction are unchanged.

Validation: **673 tests passed across 40 files, zero failures, with file retries disabled**, covering the entire Telegram platform suite, local/Mongo tenants, tenant authorization/tool boundaries, gateway multiuser policy, and all 73 HF persistence/snapshot/supervisor tests. New fallback regressions failed before the implementation fix; coverage includes allowed owners, allowed nonowners, unlisted users, rejected rich blocks, rejected collapsed entities, emoji offsets, safe logging, and no fallback after ambiguous failure. A legacy gateway policy test's incomplete `__new__` adapter fixture now mocks native welcome/reaction transport boundaries as well as `send`; no production policy change was needed. These are offline mocked-transport results, not live Telegram acceptance or rendering validation. No HF deployment/rebuild, gateway restart, credentials, or provider settings were changed.

#### Warm welcome and newly granted-user notices

Tested source `ac2d544127e206ffac31b02f8d597dee4f66fe94` removes the standalone bot-name heading and opens with **Hey {full display name}!** Names remain literal native RichText, never interpreted as markup. A concise introduction, three native capability bullets, and a `/help` hint follow. Only unlisted senders receive the gentle not-recognized line and a real, initially collapsed `PageBlockDetails` titled **Let's get you connected**, with the `@AgainOwner` access link. The existing definite-rejection fallback preserves all three bullets and the collapsed quote's UTF-16 offsets. Authored messages and button labels contain no emoji; the requested unauthorized `🥴` reaction is unchanged.

The owner-facing `/allow` success card uses explicit HTML paragraphs and escaped display names, so a name cannot swallow the following prose into a heading. It says the person can now message Naina directly and that their conversations and workspace stay separate. After the owner confirms a **new** successful grant and persistence succeeds, the recipient receives a direct RichMessage with a warm introduction, capability bullets, and a **Show me around** button using the existing safe help callback. Confirmation rechecks the started-user record and banned status. Failed, cancelled, expired, cross-owner/cross-chat, and repeated already-allowed grants send no notice; a fresh grant after revocation does. Delivery failure logs only the recipient ID and a bounded error class/RPC symbol with troubleshooting guidance, never the request body or credentials, and does not undo access or trigger an ambiguous resend. Existing grants, including Mike's, receive no retroactive message.

Final validation: **739 passed across 46 files, zero failures, automatic file retries disabled**, using four workers. Coverage includes the complete Telegram platform suite, local/Mongo tenants, authorization generations and tool boundaries, gateway multiuser/tenant policy, and all 73 HF persistence/snapshot/supervisor tests. New tests exercise literal hostile full names, rich structure and escaped HTML, the unlisted-only collapsed section, unchanged reactions, no grant from `/start`, persisted-before-notified grants, safe delivery failures, replay/cancellation/ineligible recipients, working help routing, and A-to-B-to-A profile isolation. An earlier 16-worker run exposed an intermittent failure in the untouched Mongo concurrency test; that module passed all eight tests on an isolated rerun and the final full run passed without automatic retries. The dashboard policy tests required the already-declared `python-multipart==0.0.32` test-environment dependency; no dependency files changed.

These are offline real-store/mocked-Telegram-transport results, not live Telegram rendering validation. `/approve` and `/skills` routing, `/allowall`, owner identity rules, tool isolation, credentials, provider settings, the deployed Space, and the running gateway are unchanged. No deployment, HF rebuild, gateway restart, or retroactive recipient message was performed.

#### Earlier access cards and unlisted welcome (superseded below)

Tested source `c92d6cacbdd1ae91d4378ecb02aded2f34686a7f` supersedes the earlier welcome layout above. `/disallow` now resolves the revoked person's display name from authenticated started-user metadata or stored global-grant profile data, then attempts an exact-ID Telegram lookup only if needed. Metadata failures do not prevent revocation; an unresolved name falls back explicitly to **Telegram user {ID}**. Names are escaped in a native HTML RichMessage. The group guest-access distinction remains accurate, and the final content block is italic **Page 1 of 1 • Total 1**, below a divider, never between the heading and body.

The grantor sees **Access granted** followed by the named person's access scope and isolation explanation, not **You're connected** or recipient welcome prose. A new successful grant sends the separate welcome to the granted user's private peer ID, never the grantor's callback chat. The existing route already used the recipient ID; it now also rejects a resolver/cache result that is not that exact user's private peer. Delivery failure cannot undo the saved grant. Failed, repeated, cancelled, expired, or otherwise ineligible grants still send no recipient notice; no retroactive messages are sent.

Unlisted `/start` has its own layout without any `/help` prompt: a literal full-name greeting, Naina introduction, three capability bullets, and a native highlighted blockquote with bold **btw** followed by **I don’t recognize you in this profile just yet — let’s get you introduced.** An initially collapsed `PageBlockDetails`, **Want to meet me?**, explains how to ask to be added to this profile. Its nested native `TextUrl`, **MY OWNER / @AgainOwner**, opens `https://t.me/AgainOwner`; no unsupported HTML details or fake buttons are used. Allowed-user `/start` retains its `/help` hint. Authored copy contains no emojis, and the existing unauthorized reaction is unchanged. The definite-rejection fallback retains literal names, bold text, owner URL entities, capability bullets, and UTF-16-safe collapsed-quote offsets. If only quote support is rejected, basic bold/link entities are retained before a final plain-text fallback; ambiguous failures do not create a new fallback send.

Final validation: **776 passed across 47 files, zero failures, automatic file retries disabled**, using `scripts/run_tests.sh -j 4 --file-retries 0`. This covers the complete Telegram platform suite, local/Mongo tenants, authorization and tool boundaries, tenant authorization generations and continuation, gateway admission/tenant runtime, and all 73 HF persistence/snapshot/supervisor helper tests. Regressions verify name resolution and escaping, the final italic footer, grantor-facing copy, actual peer resolution and wrong-peer suppression, distinct welcome layouts, owner links, progressive fallback, and unchanged access policy. An initial full run caught one stale copy assertion after the owner instruction was clarified; it was corrected before this final clean run. These are offline real-store/mocked-transport checks, not live Telegram rendering acceptance. `/approve`, `/skills`, owner identity, banned/revoked handling, `/allowall`, isolation, and no-host-sudo guarantees remain unchanged. No deployment, HF rebuild, gateway restart, or live demo/retroactive message was performed.

### Earlier owner-private start notice (superseded below)

Tested source `0f6a8f49d26e7d4ef1b02af7cff2d75a4316c134` replaces only the owner's notification for an unallowed user's private `/start`. The native HTML RichMessage has no heading: **{display name} (`{numeric UID}`) started {bot name}.** followed by **Actions you can take:**. Display and configured bot names are escaped literally; the bot name falls back to Naina. A single native inline button row contains red **Ban** and green **Allow**, without emojis or the old pending-access copy.

These controls are sent only to the owner's private chat and validate the actor, private chat ID, and owning tenant store again on callback. **Allow** opens the existing owner-only confirmation flow; merely viewing the notice or pressing its initial Allow button grants nothing. **Ban** uses the existing owner-only ban command policy. Repeated starts leave the user pending. The separate unlisted `/start` layout, allowed-user start notification, `/allow` success card, and recipient-DM notice remain unchanged.

Validation: **1,439 passed, 3 skipped**, covering all Telegram platform and gateway Telegram tests plus local/Mongo tenants, tenant authorization generations, Telegram roles, and gateway access policies. Tests exercise native rich serialization, literal names, UID/bot name, no header or pending copy, button row/styles, both unauthorized action routes, pending semantics, and real-store allow/ban paths. The local test environment's missing declared `python-socks` dependency was installed before the final clean run; no dependency files or unrelated code changed. These are mocked-transport tests, not live Telegram rendering acceptance. No deployment, rebuild, restart, or actual bot message was performed.

### Current access-message corrections and embedded RichMessage buttons

Tested source `ddaee60b4c4652e2b384741d4a3b73bf57973195` supersedes the earlier unlisted welcome and owner-notice layouts above. Only these three corrections are included:

- `/disallow` retains the revoked person's resolved, escaped display name and accurate global-revocation/group-guest distinction. Its page-index footer and the divider immediately before it are removed.
- Unlisted `/start` begins with plain **I don’t recognize you in this profile. Ask the owner for access.** There is no greeting, introduction, capability list, direct owner name/username, or bold `btw`. The existing initially collapsed **Want to meet me?** section contains the short access instruction and a real nested `PageBlockButtonRow` / `PageButton` labelled **Contact owner**. The button uses `InputInlineButtonTypeUserProfile` with the canonical profile owner's resolved `InputUser` and access hash, not a URL, href, `TextUrl`, text link, or regular reply keyboard. `noautolink=True` prevents implicit text links. The installed pinned Telethon 1.45.0 schema supports these constructors; a serialization/reader round-trip regression checks the exact owner ID and access hash. The unlisted rich-button message is never silently downgraded to text, a URL, or a keyboard on rejection; failures are safely logged. Allowed-user `/start` and its existing definite-rejection text fallback are unchanged.
- The pending-user notice sent to the owner's private chat uses native blocks with literal display/bot names, a fixed-width numeric ID, the unchanged start/action wording, and a `PageBlockButtonRow` embedded inside `InputRichMessage`. Red **Ban** and green **Allow** are real `PageButton` callbacks with `RichButtonStyle`, not `ReplyInlineMarkup` or an ordinary keyboard beneath the message. Their existing callback payloads, owner/private-chat authorization, Ban policy, Allow confirmation, and recipient-notification behavior are unchanged.

Final validation: **752 passed, zero failures**, with pytest configured without automatic retries. This covers the complete Telegram platform suite; local/Mongo tenant stores; tenant tool boundaries and authorization generations; gateway admission, tenant production/runtime/continuation; and HF persistence, snapshot, and supervisor tests. Focused regressions cover the removed footer/divider, direct unlisted wording with no owner name/greeting/href, nested profile navigation and schema round-trip, embedded styled admission callbacks with regular markup absent, safe refusal to downgrade the rich-only profile button, unchanged `🥴` reactions, grants, confirmations, and isolation. These are offline real-store/mocked-transport checks, not live Telegram rendering or server acceptance. No demo or retroactive messages, deployment, HF build, or local gateway restart were performed.

| Build input | Default | Where to set it |
| --- | --- | --- |
| `HERMES_REPO_URL` | `https://github.com/filewrap/hermes-agent.git` | Space variable / Docker `--build-arg`; never include credentials. |
| `HERMES_REF` | `a0923910e33f78f3487311a202c8803934c879b7` | Docker build argument only; pinned default. Override only with a separately reviewed full commit SHA. |
| `GITHUB_TOKEN` | None | Space **build secret** / BuildKit `--secret`; read-only Contents access to private source. Not an application runtime setting. |

For a different reviewed source commit locally, add `--build-arg HERMES_REF=FULL_40_CHARACTER_COMMIT_SHA` to the build command. Do not change the source ref to the dataset snapshot commit: they belong to different repositories.

### Private GitHub repository authentication

If the source repository is private, create a GitHub fine-grained token scoped only to that repository with **Contents: read**. In the Space's **Settings → Variables and secrets**, add it as a **secret**, using the Dockerfile's build-secret name `GITHUB_TOKEN`.

BuildKit exposes the secret only to the clone step through `/run/secrets/GITHUB_TOKEN`. The clone authentication uses an askpass helper rather than embedding the token in the remote URL. Never put a token in `ARG`, an ordinary public variable, the Git remote URL, a committed Dockerfile, or a `RUN echo` statement. Do not turn on shell tracing around credentials.

HF secrets are also available as runtime environment variables. A build-secret mount prevents persistence in image layers; it does **not** mean the Space's runtime environment is inaccessible to the application. Because this application can run commands, treat every secret available to the Space as available to its authorized agent. Keep the source token least-privilege and separate from dataset and provider tokens.

If you change the source repository, check its compatibility and lockfile. The HF helper layer does not make arbitrary upstream versions compatible.

### System and Python packages

The image uses a Debian-based runtime compatible with the application's supported Python range (`>=3.11,<3.14`). It includes build tools and common command-line/media/network packages, `sudo`, `tini`, `git`, `curl`, and `uv`. The Dockerfile explicitly lists what is installed; optional integrations are not implied by the phrase “Hermes and services.”

The SQLite library is built and checksum-verified at a version that retains the production Dockerfile's WAL-reset fix and FTS5 support. Do not replace it with an arbitrary older distro SQLite just to shorten the build.

Python dependencies come from the selected source revision's `pyproject.toml` and `uv.lock`, with an explicit selection of extras. Do **not** use `--all-extras`: this repository also has platform-specific, reinforcement-learning, and other heavyweight extras that are inappropriate for a basic hosted gateway.

The HF front door uses Python's standard library. It does not need Gradio, a second HTTP framework, or a dashboard build to satisfy the Space's app-port requirement.

Do not assume a Docker daemon, nested privileged containers, systemd, GPU access, inbound SSH, or every optional browser binary exists on an HF Space. Use the local terminal backend for this trial; this grants tools execution inside the same container and is a security decision.

## Runtime settings and first gateway launch

### Start in diagnostic-only mode

The default is intentionally **unconfigured**. The front door comes up, but Hermes is not launched until you explicitly select a supported state mode. This lets you see that the image starts without accidentally activating your real bot or pretending that durable state exists.

During this stage:

```text
GET /           -> deployment status, not a chat page
GET /healthz    -> front-door liveness
GET /readyz     -> HTTP 503 while the gateway is not running
```

A Space that says “Running” may simply have a healthy front door. It is not an end-to-end Hermes success report.

### Explicitly accept container-local state loss

For a disposable HF trial, set these **Space variables**:

```text
HF_STATE_MODE=ephemeral
HF_ALLOW_EPHEMERAL=1
HERMES_HOME=/home/Hermes/.hermes
```

`ephemeral` is a runtime mode value, not a secret or API key. These two settings are the gateway launch gate: they allow Hermes to use its live profile files in container-local `/home/Hermes/.hermes` and acknowledge that those files can be lost. They do **not** activate MongoDB or bucket storage. The supervisor still requires this acknowledgement even when either backup path is configured, because HF chat SQLite and other local live files remain in the container. A mounted bucket receives backup snapshots only; it is not a live SQLite data directory. Local files can disappear on rebuilds, restarts, stops, hardware changes, or platform replacement.

Do not set `HERMES_HOME=/data/.hermes`, point it through a symlink to a bucket, or put a mounted dataset at that path. The HF startup path rejects unsafe live-state placements instead of silently switching to another directory.

| Runtime control | Default / behavior |
| --- | --- |
| `HF_STATE_MODE` | `unconfigured`: HTTP diagnostics only. `ephemeral` explicitly selects container-local live state. `local` is an alias; neither mode activates Mongo or a bucket. |
| `HF_ALLOW_EPHEMERAL` | Must equal `1` to acknowledge possible loss before the gateway can launch, even if Mongo or bucket backups are configured. |
| `HERMES_HOME` | `/home/Hermes/.hermes`; must pass the local-path and filesystem checks. |
| `HF_PORT` | `7860`; if changed, update the root Space card's `app_port` too. |
| `HF_CONFIG_FILE` | Optional existing absolute seed-config path; copied without overwriting an existing config. Leave unset for automatic first-run configuration. |
| `HF_RESTORE_ARCHIVE` | Optional absolute trusted snapshot path; restore into empty local state before configuration and gateway launch. |
| `OWNER_TELEGRAM_ID` | Canonical positive decimal Telegram user ID for the owner-only HF controls; set as a Space Variable or Secret. |
| `TELEGRAM_ALLOWED_USERS` | Optional, strongly recommended comma-separated allowlist of canonical positive decimal Telegram IDs; set as a Variable or Secret. |
| `BUCKET_NAME` | Hugging Face bucket owner/name, for example `owner/name`; set as a Space Variable to namespace snapshots. |
| `MONGODB_DATABASE` | Optional Mongo database name; defaults to `hermes`. Use a unique name for isolated migration tests. |

These deployment controls belong only to this layer; changing a Space variable/secret can restart the app. Make necessary changes together where practical and expect the local-state lifecycle to apply. Existing pre-rename owner and Mongo settings remain read-only compatibility aliases; use the names above for new configuration.

### First-run configuration (automatic)

For a fresh trial using `Dockerfile.hf` and `deploy/huggingface/`, you do **not** need to invent a config file, run interactive setup, or mount a dataset. Leave `HF_CONFIG_FILE` and `HF_RESTORE_ARCHIVE` unset. After the explicit state opt-ins above pass, the supervisor creates `$HERMES_HOME/config.yaml` at runtime with file mode `0600` inside the `0700` home:

```yaml
model:
  provider: openrouter
  default: openrouter/free
  base_url: https://openrouter.ai/api/v1
terminal:
  backend: local
```

The HF bootstrap pins `openrouter/free` for the main model and available auxiliary tasks, with zero-price caps and no provider/model fallbacks. The excerpt above omits the auxiliary policy entries for brevity. Startup rejects incompatible restored routing, and request guards enforce the same policy at dispatch. Free endpoint availability is not guaranteed. No environment values, API keys, bot tokens, or sessions are written into the generated config or baked into the image; supply credentials through Space secrets.

Existing or restored config is preserved. A requested seed file takes precedence over generation for a fresh home. An existing empty config or an explicitly missing seed path is still an error, not permission to replace your settings. To adopt this fix, update the Space's copied `deploy/huggingface/supervisor.py` along with the helper package; changing only the source `HERMES_REF` does not update those Docker `COPY` inputs.

### Optional custom non-secret configuration

A minimal `config.yaml` for a remote model provider might contain:

```yaml
model:
  provider: openrouter
  default: openrouter/free
  base_url: https://openrouter.ai/api/v1
terminal:
  backend: local
```

Keep the free model, canonical endpoint, zero-price restrictions, and disabled fallbacks unchanged. For a seed file, start from `deploy/huggingface/free_policy.py::fresh_config()` so all required auxiliary guards are included; the excerpt alone is not a complete policy configuration. Keep API keys out of YAML. For unrelated settings, consult `cli-config.yaml.example` at the **same source commit** you are deploying.

If you need a custom seed, export that complete configuration to `config.yaml`, apply only reviewed non-routing settings, and use the optional dataset examples below to make it available inside the container. Automatic bucket persistence needs no seed or Dataset. A read-only seed dataset, if deliberately selected, can be used with:

```text
HF_CONFIG_FILE=/mnt/hermes-seed/config.yaml
```

Setting `HF_CONFIG_FILE` alone does not create or upload a file; it must name the real mounted path, not a path on your computer. The helper copies configuration into the local home before launching; the mounted dataset remains read-only. Do not assume later edits to the mounted file hot-reload the running gateway. Review the startup script's no-overwrite behavior when recovering an existing home.

### Runtime secrets and owner settings

Add credentials through **Space Secrets**, not files committed to either repository. The default generated config routes through OpenRouter's free model, so a usable Telegram/model trial needs:

| Name | Kind | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Secret | Needed for the default OpenRouter free-model route. |
| `TELEGRAM_API_ID` | Secret | Telegram application API ID from `my.telegram.org/apps`. |
| `TELEGRAM_API_HASH` | Secret | Telegram application API hash. |
| `TELEGRAM_BOT_TOKEN` | Secret | The bot token issued by BotFather. |
| `MONGODB_URI` | Secret | Required to use an already migrated/activated Mongo-primary profile; a URI alone never activates Mongo. It is not needed merely to start an ephemeral local trial. |
| `MONGODB_DATABASE` | Variable | Optional target database name; unset preserves the `hermes` default. |
| `BUCKET_NAME` | Variable | Required bucket identity in `owner/name` form when enabling mounted bucket snapshots. |
| `HF_TOKEN` | Optional Secret | Only for `/hf` remote bucket metadata checks or other explicit Hub API access; not required for an attached `/data` snapshot mount. |

Hugging Face automatically injects the canonical `SPACE_ID`; do not add or override it. Set `BUCKET_NAME` to the owner/name of the bucket actually attached as a **read/write mount at `/data`**. Both full-profile and chat snapshots require a valid `SPACE_ID`, valid `BUCKET_NAME`, an exact `/data` mount, and a successful write/read-back probe. A bucket name alone never creates, mounts, or enables writes; a plain `/data` directory remains ephemeral.

The one-time Mongo migration is separate from Space startup. Review the migration plan, stop the gateway for the apply step, migrate the exact owner profile, verify the migration, and install the activation marker; only then does the matching `MONGODB_URI` make Mongo primary for non-chat state. `MONGODB_DATABASE` selects the target database and defaults to `hermes`; never run an isolated test against that production database. The migration does not move chat/message rows from local SQL into Mongo. See the [MongoDB-primary migration and backup guide](website/docs/user-guide/features/mongodb-sync.md). Do not treat setting a URI as a migration or activation.

This fork uses **Telethon/MTProto** for Telegram, so a bot token alone is not the whole credential set. Do not substitute a Bot API-only setup guide.

Use a **separate test bot** if possible. Never run two competing copies of the same bot or copy a production Telethon session into the trial while production is using it. Keep production stopped or isolated according to your own operational plan; this guide does not stop, start, or migrate it for you.

Use an explicit allowlist and inspect the gateway's access-control settings. Do not assume the Space being private controls who can send messages to the Telegram bot.

Set only the credentials for the integrations you actually want enabled. Do not inject all of your saved secrets into the Space.

### HF Telegram owner bridge and profile scope

The HF supervisor passes an internal, in-memory runtime environment payload to the gateway child process, pinned to the exact launch home. `gateway.mongo_multiuser._profile_env` overlays the payload's owner and, only when explicitly supplied, allowlist for that launch home alone. The bridge never writes `OWNER_TELEGRAM_ID` or `TELEGRAM_ALLOWED_USERS` to `.env` or any other file: an existing `.env` remains byte-for-byte unchanged, including permissions. A conflicting explicit owner or allowlist in the launch profile fails before gateway launch; values are compared rather than merged, so the bridge never combines allowlists or grants. Matching allowlists are compared as numeric sets (order and duplicates do not matter). Invalid/noncanonical IDs, leading zeroes, wildcards, and non-positive values are rejected. If no owner is supplied, there is no bridge overlay. Raw `OWNER_TELEGRAM_ID` or `TELEGRAM_ALLOWED_USERS` variables without the supervisor's internal runtime marker do not alter ordinary local behavior.

The internal payload is not logged, written to config, or included in snapshots. It applies only to the exact launch home: other and routed profiles do not inherit the launch owner's identity or allowlist. The owner identity is separate from admission: existing tenant access-state/ban persistence is unchanged, and startup does not reset durable bans or revocations; the owner can admit a numeric ID with `/allow`. Bridge bootstrap settings themselves are not persisted. `MULTIUSER_ENABLED` is retired and is not part of this setup.

Validation should cover that bootstrap settings never reach disk (including `.env`), logs, config, or snapshots; conflicts fail before launch without merging grants; and A-B-A profile isolation (launch profile A, run profile B, then return to A) prevents cross-profile inheritance. It should also cover absent owner, owner-only and owner-plus-explicit-allowlist payloads, canonical-ID validation, equivalent allowlist sets, and unchanged `.env` bytes and permissions. These are startup-contract checks, not evidence of a live Space deployment.

### Telegram access and model controls

Ownership is the profile's canonical `OWNER_TELEGRAM_ID`, never membership in an allowlist. `/logs`, its download buttons, `/hf`, and `/msettings` are owner-only and private-chat-only. The Model settings button in a group shows a DM-only alert without changing settings. Native settings navigation edits the existing RichMessage, includes page indexes, and reuses the current card's catalog metadata; a changed model route or configuration requires fresh `/model` controls.

The owner can inspect all global grants with `/allowlist` in DM, or `/allowlist 2` to open a particular 1-based page. Previous/Next buttons edit the paginated RichMessage. `/disallow <numeric user id>` revokes that exact account's global access; it cannot revoke the owner. Revocation is durable, and reopening the store does not reseed an already-revoked configured allowlist entry.

`/allowall` and `/gsettings` apply only to their current group/forum and remain available to the owner and authorized users there. Their selected controls use button color, without emoji checkmarks. Allow all admits unlisted guests only to that group; it never grants DM/global access, and switching it off does not withdraw the owner or explicitly allowed users' existing access. Invocation mode still applies. Provider screens, provider authentication, credential acceptance, model routing, and the free-model policy are unchanged by these controls.

### Verify in layers

1. Build log: the source clone, checksum checks, lockfile install, and image build completed.
2. Runtime log: the front door bound to the configured port.
3. `/readyz`: the gateway subprocess is running; read the endpoint's stated limitations.
4. Gateway logs: the intended platform adapter connected, with no authentication, schema, or filesystem errors.
5. Send a harmless message from an allowed Telegram account and confirm an actual model response.
6. Check the runtime home path and local files through an authorized terminal, without printing secret contents.
7. Test snapshot creation and a restore drill into a **different empty directory** before entrusting valuable state.

The supervisor runs the real `hermes gateway run` entrypoint. It is not an interactive `hermes` REPL left waiting on stdin. If the subprocess fails, the deployment must not remain indefinitely “healthy” behind a dummy web server.

The image uses `ENTRYPOINT` rather than a second unsupervised background shell command: `tini → hf-hermes-start → supervisor.py → gateway subprocess`. The shell script uses `exec`; the supervisor owns both the HTTP server and the gateway lifecycle, forwards shutdown to the gateway process group, and treats an unexpected child exit—even exit code zero—as a deployment failure. This is deliberately separate from the production image's s6 bootstrap and hard-coded `/opt/data` wrappers. No application update, package install, or source clone is deferred to startup.

## Private dataset repositories

A private **dataset repository**, a **bucket**, and a **Space repository** are different resources:

| Resource | Best use here | Writable through an attached mount? |
| --- | --- | --- |
| Space repository | Docker build instructions and non-secret helper code | Not a runtime database. |
| Private dataset repository | Versioned seed config or immutable snapshot archives | **No: dataset mounts are read-only.** |
| Private bucket | Snapshot artifacts, exports, large files | Yes, if mounted read-write; not validated for live SQLite. |
| Container-local disk | Active Hermes home and live databases | Yes, but ephemeral on HF. |

### Create a private dataset

In the HF UI, create a dataset under your namespace, for example `YOUR_HF_NAME/hermes-private-state`, and select **Private**. For an organization, make sure your account and token have the required access.

Alternatively, run this from an authorized management computer. Use a **separate management environment** rather than upgrading Hermes' locked dependencies in place:

```bash
uv venv .venv-hf-admin
uv pip install --python .venv-hf-admin/bin/python 'huggingface_hub==2.2.0'
source .venv-hf-admin/bin/activate
hf auth login
```

```python
from huggingface_hub import HfApi

api = HfApi()  # Uses your authenticated local HF account.
api.create_repo(
    repo_id="YOUR_HF_NAME/hermes-private-state",
    repo_type="dataset",
    private=True,
    exist_ok=False,
)
```

`exist_ok=False` deliberately stops if the name already exists. `private=True` on a create call is **not** a blanket guarantee that an existing repository was private already. Verify visibility in the UI before any sensitive upload. HF SDK versions evolve; the management examples use the documented `Volume` API and are separate from the application's locked SDK version.

### Token scopes and identity

Use a fine-grained HF token with access only to the intended dataset/bucket:

- Seed downloads need **read** access.
- Snapshot uploads need **write** access to the destination dataset.
- Updating a Space's volumes requires permission to manage that Space as well as access to the source resource.
- A runtime `HF_TOKEN` does not change repository visibility and is not an encryption key.

Give management permissions to your management environment, not automatically to the deployed AI agent. If the Space only reads a platform-managed mounted dataset and does not call the Hub API, it may not need a runtime token at all; the identity attaching the volume still needs access.

Do not put private dataset IDs in public metadata just for decoration. A mount's source may be masked for users without access, but mount paths and mode can still be visible. Privacy is not the same as encrypted archival storage.

### Upload a seed file or a closed archive

```python
from huggingface_hub import HfApi

api = HfApi()
api.upload_file(
    repo_id="YOUR_HF_NAME/hermes-private-state",
    repo_type="dataset",
    path_or_fileobj="./config.yaml",  # Non-secret configuration only.
    path_in_repo="config.yaml",
    commit_message="Add reviewed Hermes test configuration",
)
```

A snapshot is uploaded the same way, but use an immutable, unique name such as `snapshots/hermes-20261019T020000Z.tar.gz`. Do not upload a live `state.db` with `upload_folder`, `CommitScheduler`, `rsync`, or a background file copier. Upload **finished snapshot artifacts**, not files that are changing underneath the uploader. See the official [file upload guide](https://huggingface.co/docs/huggingface_hub/guides/upload).

## Attach mounted storage

### Recommended: the private bucket already selected

In the Space's **Mount a bucket** dialog, attach the private bucket whose owner/name matches `BUCKET_NAME` at **`/data`**, with **Read & Write** access. Set `BUCKET_NAME=owner/bucket-name` as a Space Variable. No Dataset is needed. Keep `HERMES_HOME=/home/Hermes/.hermes`, `HF_STATE_MODE=ephemeral`, and `HF_ALLOW_EPHEMERAL=1`; the explicit ephemeral acknowledgement still applies to unsnapshotted changes and failed or unavailable mounts. Leave `HF_RESTORE_ARCHIVE` unset for automatic bucket recovery. Full-profile and chat snapshot writes require the platform-provided `SPACE_ID`, a valid `BUCKET_NAME`, `/data` as an exact mountpoint, and a successful write/read-back probe. The bucket name alone or an ordinary VPS directory cannot enable the bucket path.

When you later choose to update the Space, copy the reviewed `Dockerfile.hf`, `Dockerfile.hf.dockerignore`, and the complete `deploy/huggingface/` helper directory to its build repository. Updating only the GitHub source pin does not update files brought in through Docker `COPY`. This patch does not deploy, restart, or attach storage itself.

The supervisor checks the platform `SPACE_ID`, a valid `BUCKET_NAME`, an exact `/data` mountpoint, and a successful write/read-back probe before full-profile backup or restore. Every **60 seconds**, and after the gateway stops during graceful shutdown, it publishes a complete profile ZIP under `/data/hermes-full-state/{owner}/{bucket}/` when content changes. It includes credentials and `.env`, settings, skills, all non-chat state, local chat/message history, SQLite databases (including Telethon `.session` databases), and other durable profile files; SQLite databases are copied with the backup API and integrity-checked. The archive is read back and checksum-verified before its commit marker is published. Identical profiles are deduplicated and the **three newest validated snapshots** are retained. Partial, uncommitted, or corrupt snapshots are not recovery candidates. These archives are not encrypted by Hermes and contain secrets: keep the bucket private and access-limited.

At startup, before the gateway opens its database, recovery tries the newest valid full-profile snapshot and restores only files absent from the local home; existing local files are never overwritten. If `HF_RESTORE_ARCHIVE` is explicitly set, that archive takes precedence over automatic bucket recovery. No valid mounted bucket snapshot is an unavailable/ephemeral backup, not a signal to write to an unmounted `/data` directory. The existing `openrouter/free` zero-price/no-fallback guards remain enforced.

Check private runtime logs and the `detail` field on `/readyz` or `/`: they report whether bucket snapshots are available, startup files were restored, or the profile remains local/ephemeral. Until `SPACE_ID` and `BUCKET_NAME` are valid and `/data` is mounted and writable, no bucket writes occur; if any snapshot fails, the gateway retains local behavior and reports the risk. Until a valid snapshot is published—and whenever mounted writes have not yet reached remote storage—recent data can still be lost. Use only one Space writer for this snapshot directory.

### Dataset mounted read-only

From your authenticated HF-management environment:

```python
from huggingface_hub import HfApi, Volume

api = HfApi()
space_id = "YOUR_HF_NAME/hermes-test"
current = api.get_space_runtime(repo_id=space_id).volumes or []

# REVIEW the current list first. This example REPLACES that list.
print(current)
api.set_space_volumes(
    repo_id=space_id,
    volumes=[
        Volume(
            type="dataset",
            source="YOUR_HF_NAME/hermes-private-state",
            mount_path="/mnt/hermes-seed",
            read_only=True,
        ),
    ],
)
```

**Warning:** `set_space_volumes` replaces **all** previously mounted volumes. To retain an existing volume, include it in the new list. Do not copy this example unchanged onto a Space with important mounts. Volume changes can affect the running Space; plan them as deployment changes, not harmless reads.

Dataset/model/Space repository volumes are always read-only. A dataset cannot be made writable just by setting `read_only=False`. For writes, use the Hub API to make a dataset commit or attach a writable bucket.

### Create a private bucket and mount it at `/data`

Create a private bucket through [New bucket](https://huggingface.co/new-bucket), or in your authorized management environment:

```python
from huggingface_hub import create_bucket

create_bucket("YOUR_HF_NAME/hermes-artifacts", private=True)
```

Then attach both resources in the complete desired volume list:

```python
from huggingface_hub import HfApi, Volume

api = HfApi()
api.set_space_volumes(
    repo_id="YOUR_HF_NAME/hermes-test",
    volumes=[
        Volume(
            type="dataset",
            source="YOUR_HF_NAME/hermes-private-state",
            mount_path="/mnt/hermes-seed",
            read_only=True,
        ),
        Volume(
            type="bucket",
            source="YOUR_HF_NAME/hermes-artifacts",
            mount_path="/data",
            read_only=False,
        ),
    ],
)
print(api.get_space_runtime(repo_id="YOUR_HF_NAME/hermes-test").volumes)
```

You can also attach buckets from the Space creation/settings UI. The official [Space storage guide](https://huggingface.co/docs/hub/en/spaces-storage), [volume API guide](https://huggingface.co/docs/huggingface_hub/guides/manage-spaces#mount-volumes-in-your-space), and [bucket documentation](https://huggingface.co/docs/hub/en/storage-buckets) cover current options.

### Check a runtime mount without touching existing data

In the actual Space container, inspect metadata first:

```bash
id
stat -c '%u:%g %a %n' /home/Hermes /data
findmnt -T /data
findmnt -T /home/Hermes/.hermes
```

To test direct writes, choose a unique throwaway file and clean up only that file:

```bash
probe="$(mktemp /data/hermes-write-probe.XXXXXXXX)" || exit 1
printf 'Hermes write test\n' > "$probe"
cat "$probe"
rm -- "$probe"
```

A successful write proves that this user can write that file. It does **not** prove SQLite lock, mmap, fsync, rename, or crash-recovery correctness. Avoid `chmod -R 777 /data`, recursive `chown` of an existing mounted dataset/bucket, or attempts to repair unknown production data.

If HF supplies ownership different from UID 1000, fix mount/access settings where supported, or use the documented upload API. Sudo inside the container may not override ownership semantics enforced by the remote filesystem.

### Binding local storage later

On a Docker host you control, you can bind a local directory to `/data`:

```bash
mkdir -p ./hf-artifacts
# On Linux only, arrange the directory ownership for the image's UID/GID.
# Review this path before chowning; do not run against an existing data root.
sudo chown 1000:1000 ./hf-artifacts
docker run --rm -p 127.0.0.1:7860:7860 \
  --mount type=bind,src="$(pwd)/hf-artifacts",dst=/data \
  hermes-hf:test
```

That makes `/data` writable artifact storage; it does not automatically select it as Hermes' live home. On HF, your laptop's path cannot be bind-mounted into the hosted Space. Attach an HF-managed volume instead.

### Why live SQLite is deliberately not placed on the bucket

HF-managed bucket mounts use `hf-mount`-style NFS/FUSE access. The project's [filesystem documentation](https://github.com/huggingface/hf-mount#consistency-model) describes eventual consistency, write buffering, and limitations on cross-client locking. Local advisory locks on one mount do not establish a general SQLite durability guarantee.

SQLite explicitly says [WAL does not work over a network filesystem](https://www.sqlite.org/wal.html). Its [network filesystem guidance](https://www.sqlite.org/useovernet.html) also warns about locking and sync reliability. Changing `journal_mode` to `DELETE`, running a one-file smoke test, or successfully restarting once is not sufficient evidence of safe production behavior.

Before ever considering a different deployment that writes live SQLite to a target mount, it would require target-Space tests for process-level fcntl locking, concurrent readers/writers, mmap/shared memory behavior, fsync ordering, atomic rename, interrupted writes, container termination, restore validation, and recovery after restart. Even those tests do not remove SQLite's documented caveats. This deployment does not offer a switch to bypass the restriction.

Do not attach a network/repository volume to a **subdirectory** of the live home either—for example `profiles/`, a tenant-state directory, or a database filename. A local top-level home does not make nested remote files local. Keep all attached resources at separate artifact/seed paths such as `/data` and `/mnt/hermes-seed`.

For durable, low-loss operation, choose infrastructure with a verified local/block-backed persistent filesystem, or a deliberately implemented supported client/server state backend. Merely setting a Mongo URI does not replace all local Hermes state in this fork.

## Snapshots, upload, restore, and recovery

### What the helper guarantees—and what it does not

`deploy/huggingface/snapshot.py` is a separate **manual** helper. It does not start Hermes, stop it, upload anything, create a scheduled task, or mutate the production runtime.

Creation requires a `--quiesced` acknowledgement. Stop or quiesce **all writers** to the selected home before using that flag: gateway, dashboard, CLI sessions, scheduled jobs, and any profile processes. The helper cannot prove you did this. Do not take down a live service casually just to create a test backup.

For each SQLite file it uses the [SQLite backup API](https://www.sqlite.org/backup.html), not a raw copy of a database that may depend on a WAL file. Each database gets a consistent snapshot; there is no atomic transaction covering all SQLite databases and ordinary files together. Quiescing writers provides the cross-file consistency boundary.

The helper excludes raw SQLite journal sidecars and transient runtime artifacts, rejects symlinks/special files, records file hashes, and restores only into a new empty destination. Read its CLI help for the exact filtering rules. A default snapshot is **not certified secret-free**: chat history, configuration, plugin data, and arbitrary files can contain sensitive content even if known credential files are excluded.

### Create a snapshot from a stopped test home

In the built image, the helper is copied alongside the startup scripts. From a source checkout, use its repository path:

**Path distinction:** the image's copied helper is `/home/Hermes/hf/snapshot.py`. Replace `python deploy/huggingface/snapshot.py` in the following source-checkout examples with `python /home/Hermes/hf/snapshot.py` when working inside the image. The application and copied helper revisions are independent, so use the copied helper path rather than assuming the application clone contains the same deployment files.

```bash
umask 077
# This is an EXAMPLE test home, not your production directory.
TEST_HOME=/home/Hermes/.hermes
ARCHIVE=/home/Hermes/hermes-snapshot-20261019T020000Z.tar.gz

# Only pass --quiesced after all writers to TEST_HOME are actually stopped.
python deploy/huggingface/snapshot.py create \
  --home "$TEST_HOME" \
  --output "$ARCHIVE" \
  --quiesced
```

The archive output must be outside the source home and must not already exist. Choose a unique filename for every snapshot.

Do not use the manual helper's `--include-secrets` option for bucket persistence. Credentials and Telegram authentication material must stay out of these recovery artifacts. A private HF bucket is access control, not end-to-end encryption; conversation history still needs appropriate access restrictions.

Snapshot filtering may exclude transient files that should be recreated, and it refuses symlinks instead of silently dereferencing them. If a custom skill is symlinked outside the home, inventory it and create an explicit safe packaging plan rather than weakening path validation.

Known secret exclusions include `.env`, `auth.json`, `auth_store.json`, `credentials`, `credentials.json`, `token.json`, `secrets.json`, the `auth`/`credentials`/`secrets` directory names, and `.pem`/`.key`/`.token`/`.env`/`.session` suffixes. Transient names include lock/PID/socket names and `-wal`/`-shm`/`-journal` sidecars. Empty directories are not retained. Owner-executable ordinary files restore as `0700`; other files restore as `0600`, directories as `0700`. Existing ownership, group/world access, ACLs, and extended attributes are not restored.

The helper publishes archives on **local POSIX disk** using an atomic no-clobber hard link. Create the archive locally first; a network/bucket mount that lacks hard-link semantics is not a supported snapshot output directory. Upload or copy the closed archive afterwards.

### Upload a finished snapshot to the private dataset

Use your HF-management environment and authenticate with a write-scoped token:

```python
from huggingface_hub import HfApi

api = HfApi()
info = api.repo_info(
    repo_id="YOUR_HF_NAME/hermes-private-state", repo_type="dataset"
)
if not info.private:
    raise RuntimeError("Refusing to upload private Hermes state to a public dataset")

commit = api.upload_file(
    repo_id="YOUR_HF_NAME/hermes-private-state",
    repo_type="dataset",
    path_or_fileobj="/home/Hermes/hermes-snapshot-20261019T020000Z.tar.gz",
    path_in_repo="snapshots/hermes-20261019T020000Z.tar.gz",
    commit_message="Store quiesced Hermes test snapshot",
)
print(commit.oid)  # Record the exact dataset commit for recovery.
```

Run this where the archive actually exists; a management computer cannot access a file inside an unrelated Space just because the path string matches. Transfer the archive through an authorized private channel if needed. Never publish a public download link for credential-bearing backups.

The upload creates a dataset commit. Deleting a file from the current tree is not a promise of erasing old versions or revoked copies. If a token leaks, rotate it; do not merely delete the latest archive.

### Or upload closed snapshots to a private bucket

A management CLI can upload the completed artifact without keeping an active database open on the mount:

```bash
hf buckets cp /home/Hermes/hermes-snapshot-20261019T020000Z.tar.gz \
  hf://buckets/YOUR_HF_NAME/hermes-artifacts/snapshots/hermes-20261019T020000Z.tar.gz
```

Buckets are mutable and not Git-versioned. Use unique snapshot names and a recorded external SHA-256. Do not overwrite a known good backup while testing a new one. Prefer Hub API/CLI upload plus download-and-verify for backup publication, rather than assuming an asynchronous mounted write is durable immediately after a local `cp` returns.

### Restore drill into a new local directory

Never restore over a running or nonempty home. First download a **specific dataset revision** into an ordinary local cache:

```python
from huggingface_hub import hf_hub_download
from pathlib import Path
import shutil

archive = hf_hub_download(
    repo_id="YOUR_HF_NAME/hermes-private-state",
    repo_type="dataset",
    filename="snapshots/hermes-20261019T020000Z.tar.gz",
    revision="FULL_DATASET_COMMIT_SHA",
)
local_archive = Path("hermes-restore-input-20261019T020000Z.tar.gz").absolute()
with open(archive, "rb") as source, local_archive.open("xb") as target:
    shutil.copyfileobj(source, target)
local_archive.chmod(0o600)
print(local_archive)
```

Hub caches can use symlinks. The helper deliberately refuses symlink inputs, so the example materializes a regular file with exclusive creation rather than passing a cache symlink directly. Then use that actual local archive path:

```bash
python deploy/huggingface/snapshot.py restore \
  --archive /actual/downloaded/snapshot.tar.gz \
  --home /home/Hermes/restore-drill
```

The helper validates the archive's manifest, checksums, member paths/types, and SQLite integrity before completing a new local restore. It does not merge state, selectively overwrite a profile, or resolve conflicts.

Checksums detect accidental changes; an attacker who can replace the archive can also replace its internal manifest. Pin the dataset commit and, for stronger trust, verify an expected archive checksum or signature kept separately. Treat restored skills/plugins/config as executable or security-sensitive input, not inert data.

Inspect the restored structure, verify expected profile files, and confirm counts/content using read-only tools. Do not activate the same bot/session in parallel with the original just to test recovery.

### Bootstrap a fresh Space from a mounted snapshot

If the dataset is mounted at `/mnt/hermes-seed`, select one immutable archive with the optional runtime input:

```text
HF_RESTORE_ARCHIVE=/mnt/hermes-seed/snapshots/hermes-20261019T020000Z.tar.gz
```

Restore must target the approved **local** home before the gateway starts. It is not permission to use the read-only dataset as a live home. Restoration fails on nonempty state rather than merging or silently clobbering it. Verify logs and the restored home before sending real traffic.

Do not point this setting to an encrypted file, an in-progress upload, an untrusted archive, or a moving “latest” object. The restored snapshot is a baseline: changes since that snapshot remain at risk until a newer verified snapshot is published.

### Backup frequency and recovery limits

The HF supervisor's automatic bucket snapshots are separate from the manual upload examples above. No external scheduler, local systemd service, or Dataset is required. A shutdown-only backup is insufficient: hard kills, platform restarts, timeouts, network failures, or process crashes can prevent it, so periodic snapshots are also required.

Use one Space writer per bucket snapshot directory. Do not run a raw `upload_folder(HERMES_HOME)` loop against active SQLite and call that a backup system. Verify recovery in the actual mounted Space before trusting it with irreplaceable history.

Assume **all work after the last valid remotely available snapshot may be lost**. A successful mounted write or local checksum does not prove that HF has durably flushed the object to remote storage. If that recovery limit is unacceptable, use verified persistent local/block storage instead.

## Ports, networking, and security

### What is publicly reachable

Only the configured Space **`app_port`** is routed by HF's normal HTTPS front door. The image binds its deployment endpoint to `0.0.0.0:7860`; the Space card must agree.

```text
https://YOUR-HF-SPACE-SUBDOMAIN.hf.space/
https://YOUR-HF-SPACE-SUBDOMAIN.hf.space/healthz
https://YOUR-HF-SPACE-SUBDOMAIN.hf.space/readyz
```

Use the actual App URL shown by HF; do not assume the transformation of every namespace/name into a subdomain. Private Spaces require authorized access through HF, so an unauthenticated curl may get a login/permission response rather than your application JSON.

`EXPOSE 7860` documents the port in Docker; it is not a firewall rule and does not publish arbitrary TCP listeners. Exposing `22`, a database port, the Telegram transport port, or a dashboard port does not make it internet-accessible through a Space.

Telegram MTProto and model-provider calls are outbound. They do not require opening a public bot webhook port in this setup. Actual HF networking restrictions and upstream reachability must be tested; a successfully built image proves neither.

### Multiple services and the dashboard

The supported dual startup is the lightweight HTTP front door plus the Hermes messaging gateway. It is **not** an unauthenticated dashboard, web terminal, file server, or remote shell. The default app page has no write/action API and should not reveal credentials or chat history.

If you deliberately add other HTTP services later, route them through one authenticated reverse proxy on the app port, as described in the [Docker Spaces documentation](https://huggingface.co/docs/hub/en/spaces-sdks-docker). Handle WebSockets, URL prefixes, Host/Origin validation, OAuth/public URLs, and application authentication correctly. Do not blindly proxy a localhost-only dashboard and thereby bypass its trust boundary.

The existing Hermes dashboard has its own authentication and public-host rules. This deployment does not configure or weaken them. A Space being private is an additional access boundary, not a replacement for application authorization.

### Sudo and secret exposure

Passwordless sudo was explicitly requested and is present **inside this container**. HF's host isolation still applies: sudo does not provide a privileged host, a Docker daemon, a mount capability the platform denies, or permission to bypass HF's service rules.

Nevertheless, anyone who can make the agent execute arbitrary commands may read all accessible runtime secrets and files. Use a private Space, test credentials, a narrow Telegram allowlist, and scoped tokens. Do not expose a public agent terminal. Do not bake real secrets into image layers. Avoid logging full environment dumps, request headers, connection strings, or archive contents.

## Local image checks

Run these on a machine with a working Docker/BuildKit installation. Building is separate from deploying to HF.

```bash
# If the source is private, export GITHUB_TOKEN securely in your local shell.
# The Docker command passes it as a BuildKit secret, not a build argument.
docker build -f Dockerfile.hf \
  --secret id=GITHUB_TOKEN,env=GITHUB_TOKEN \
  -t hermes-hf:test .
```

Use a reviewed source SHA through the Dockerfile's source-ref build argument when changing the application version. On HF, build arguments come from corresponding Space variables; credentials remain secrets.

Start only the unconfigured front door for the first local check:

```bash
docker run --rm --name hermes-hf-test \
  -p 127.0.0.1:7860:7860 hermes-hf:test
```

In another terminal:

```bash
curl --fail http://127.0.0.1:7860/healthz
curl -i http://127.0.0.1:7860/readyz   # Expect 503 while unconfigured.
docker exec hermes-hf-test id
docker exec hermes-hf-test sudo -n true
docker exec hermes-hf-test stat -c '%U:%G %n' /data /home/Hermes
docker exec hermes-hf-test /home/Hermes/hermes-agent/.venv/bin/python \
  /home/Hermes/hf/check_runtime.py
docker stop --time 20 hermes-hf-test
```

Do not use `-p 7860:7860` on a shared host unless you intend to bind all host interfaces. The loopback-only mapping above is safer for local diagnostics.

A real local gateway trial should use a separately reviewed private env file, disposable credentials, the explicit ephemeral acknowledgement, and a local read-only config mount. Do not point it at the stopped production home. Do not run two copies of the same Telegram session.

## Troubleshooting

| Symptom | Likely cause and next action |
| --- | --- |
| HF never detects an app | Check root `README.md` YAML, root `Dockerfile`, `app_port`, and bind address. A listener on `127.0.0.1` inside the container is not sufficient. |
| “Running” Space, but bot does not answer | Read `/readyz`, gateway logs, allowlist, Telegram credentials, and provider config. The front door is not proof of bot health. |
| Telegram command picker omits commands or logs `BotCommandsTooMuchError` | The [Bot API documents a maximum of 100 commands](https://core.telegram.org/bots/api#setmycommands); the MTProto method reference does not specify a separate count. If MTProto returns its too-many-command error, Hermes retries that same scope with smaller built-in-first prefixes and reports the exact omitted count/names. Commands omitted from the picker remain available through `/help` and normal slash-command dispatch. Scopes are alternatives selected by Telegram, not additive lists; personal skill names are registered only in that user's private scope. |
| `/readyz` is 503 with unconfigured state | Select the explicit trial state mode and acknowledgement; provide configuration/credentials. |
| Gateway exits shortly after launch | Inspect the first runtime error. Do not keep a dummy health server forever or suppress the child failure. |
| Git clone fails with 401/404 | Verify the repository URL, pinned commit, secret name, fine-grained token permission, organization authorization, and that the repo is accessible. Private repositories can appear as 404 without permission. |
| HF uvloop/runtime check fails | Confirm the build fetched the reviewed source SHA, installed its frozen lock, and runs the actual application venv with uvloop 0.22.1 on Linux CPython. Rebuild the reviewed application/helper pair; do not bypass the check or replace the gateway selector with `asyncio.run()`. |
| Clone works, dependency installation fails | Check the pinned source lockfile, Python version, package index access, platform wheels, and build tools. Do not casually unpin or upgrade every package. |
| Build cannot find README or deployment files | Use the HF-specific ignore file and copy the helper directory into the Space repository. Do not use an ignore rule that removes required build inputs. |
| Dataset download/mount returns 401/403/404 | Verify repo type `dataset`, namespace, visibility, token/account access, and selected filename/revision. Do not change the repo to public to bypass access problems. |
| Cannot write to a dataset mount | Expected: repo mounts are read-only. Write through the Hub commit API or use a bucket for artifacts. |
| Cannot write `/data` | Check that the bucket is attached read-write and inspect its runtime permissions. Build-time chown cannot repair a later mounted remote filesystem. |
| `/data` is empty after a successful build | A runtime mount hides image contents. Store source and build assets outside `/data`; upload desired runtime data to the backing resource. |
| Startup rejects `/data` or a network filesystem home | Intentional SQLite safety restriction. Use a local live home and closed snapshots; do not bypass the check with symlinks. |
| Restore refuses a nonempty destination | Intentional. Inspect/quarantine the existing test home manually and select a new empty path. Do not add an overwrite flag or merge archives over active DBs. |
| Restore fails checksum or SQLite validation | Stop. Keep the original archive, choose a known good prior snapshot, and diagnose separately. Do not launch on a partially restored directory. |
| Snapshot fails on a symlink/socket/unsupported file | Inventory that custom state and package it explicitly or remove only a known stale transient artifact after verifying no process owns it. Do not follow unknown links. |
| HF restart loses recent conversations/settings | The live home is ephemeral. Restore only recovers the last uploaded verified snapshot. This is an expected limitation, not evidence the mounted backup automatically captured recent changes. |
| Sudo cannot mount FUSE/start Docker | Container root does not grant host capabilities or a Docker daemon. Use HF-managed attached volumes and supported tool backends. |
| Bot loses connectivity after inactivity/restart | Inspect HF runtime state and platform logs; hosted Spaces are not an unconditional always-on service. Test the lifecycle required by your use case. |

### Safe diagnostics

Inspect file metadata, status codes, and minimal relevant log lines. Do **not** run `env`, `cat ~/.hermes/.env`, print a Mongo URI, dump bot session files, or paste an entire credential-bearing traceback into a public issue. Check the first failing phase before changing unrelated variables.

## Update and rollback checklist

1. Record the current application source SHA, deployment-helper commit, dataset snapshot commit/path, and current volume list.
2. Create and verify a fresh snapshot with writers quiesced, if the test contains anything worth keeping.
3. Keep the previous known good archive under a unique name. Do not overwrite it.
4. Update only the intended source build argument or reviewed HF helper files.
5. Rebuild the Space and inspect both build and runtime logs.
6. Verify front-door liveness, subprocess readiness, real messaging, authorization, and snapshot restore behavior separately.
7. Roll back code by selecting the prior source SHA/helper revision; roll back state only by an explicit restore into a clean local home.
8. Do not assume old code can read a newer database schema. Preserve a state snapshot paired with the known good code revision.

Mongo remains opt-in until an exact profile migration completes and a verified activation marker is installed. After explicit activation, Mongo is primary for non-chat state and SQLite keeps chat/message history; the HF bucket is a separate full-profile secondary backup when valid `SPACE_ID` and `BUCKET_NAME` values and a real writable `/data` mount are present. A URI or `BUCKET_NAME` alone never changes storage mode. See [the Mongo migration and backup guide](website/docs/user-guide/features/mongodb-sync.md).

## What has and has not been validated

The source baseline was inspected to confirm its existing `/opt/hermes` and `/opt/data` production layout, hard-coded wrapper paths, local canonical storage policy, Telegram credential requirements, and Python/lockfile constraints. This deployment intentionally does not reuse those production wrappers.

Automated checks for these HF helpers use temporary homes and synthetic subprocesses/SQLite databases. They do not activate Hermes or touch production state. Syntax checks and unit/integration tests are not equivalent to building the complete image on HF.

**Not yet established by these files:** a successful target-Space image build, real Telegram/model connectivity from HF, bucket filesystem suitability for active SQLite, crash-proof zero-loss persistence, or end-to-end recovery under actual HF lifecycle events. Those require your isolated target-Space trial. The existing Hermes service was not started, restarted, or deployed as part of preparing this path.

## Owner diagnostics in Telegram

Send `/logs` or `/hf` in the configured owner's private chat. Both use the existing Telethon feature-command path without starting a model turn. They do not depend on appearing in Telegram's slash menu. Normal admission and isolation still apply; non-owner tenants cannot read installation logs or deployment diagnostics.

`/logs` sends a native RichMessage showing the latest 25 lines of `gateway.log`, `errors.log`, and `agent.log`, bounded to 5,000 characters per file. **Download all retained logs (.zip)** sends the current profile's redacted `agent`, `errors`, `gateway`, and `gui` logs, including numeric rotations. The button is bound to the owner, private chat, thread, profile and message, expires after 15 minutes, and rechecks authorization when clicked. Symlinks, arbitrary files, configuration, databases, `.env` and Telegram authentication files are excluded. Deleted rotations and unrecorded supervisor stdout cannot be recovered. Exports fail explicitly instead of silently truncating at the safe limits: 256 log files, 32 MiB per file, 128 MiB combined content, and a bounded directory scan. Temporary exports are removed after delivery. Redaction reduces credential exposure, but debug/conversation text may still be private: review before sharing.

`/hf` displays **startup → database/storage → Telegram → model/providers**. It checks the local supervisor `/readyz` when HF is identifiable, the selected profile home and SQLite version, bounded read-only database/schema/row-count/quick-check/journal-mode results, a dedicated `/data` mount and temporary write probe, and measured snapshot-pair count, age and combined size. Pair presence is not checksum or restore verification; oversized/unreadable inventories are explicitly unavailable. Telegram's result is local transport status, not a live API test. Model/provider identifiers are configuration evidence only: no model/provider requests or routing changes occur.

With an existing profile-scoped `HF_TOKEN` or `HUGGING_FACE_HUB_TOKEN`, `/hf` makes one bounded, read-only [official Bucket metadata request](https://huggingface.co/docs/huggingface_hub/en/guides/buckets#get-bucket-info) for `xkailashhh/hermes-artifacts`, reporting privacy, total bytes and file count. Tokens, endpoint credentials and server error bodies are not shown. Without a token, or on access/connectivity/validation failure, remote status is explicitly unverified. Account quotas are not measured. Metadata cannot prove `/data` is that bucket or that buffered writes reached remote storage. The command never creates, mounts, deletes or repairs a bucket.

### Recovery boundaries

Chat and tenant databases remain local and are individually replaced only when their respective local history is empty. A missing tenant database can be recovered while preserving a surviving chat database. The replacements are not one cross-file transaction: if chat recovery succeeds but tenant publication fails, startup reports partial recovery and can retry the missing tenant store at a later startup. Meaningful/unreadable local state and orphaned SQLite sidecars are protected, not overwritten. Snapshot-only tenant bundle tables stay out of the live chat database, so recovery can continue writing and publishing its next snapshot. Tenant permissions and room histories are exported in one committed read view. Local restore–continue–snapshot–restore tests cover these behaviors; writable mount permissions, buffering/visibility, abrupt-stop recovery and live Telegram behavior still need validation on an isolated owner-controlled Space. These changes do not deploy or restart anything.
