# Hugging Face Spaces: future deployment plan

## Recommendation: test one owner first

Hugging Face (HF) Docker Spaces is a reasonable place to **test Docker packaging, Telegram connectivity, and restart behavior for one trusted owner**. It is **not yet established as a safe production host for multi-user full-tool Hermes**. A private Space, separate profile directories, and the worker prototypes do not establish per-identity OS isolation.

This is a future plan, not a deployment record. Adding this document does not create a Space, change runtime configuration, enable workers, or restart the existing gateway. At this planning baseline, the personal fork includes worker-boundary prototypes through `5d896e4`; the existing systemd-hosted bot intentionally remains on `22d2361`, with non-owner tenant tools denied. Keep that deployment unchanged while testing HF separately.

There are two distinct targets:

| Target | Allowed scope | Release status |
| --- | --- | --- |
| Single-owner prototype | Disposable test bot, owner-only allowlist, scoped test credentials, packaging/network/lifecycle checks | Candidate for an owner-run smoke test; not a security sandbox for untrusted work |
| Multi-user full tools | Whole-agent workers, verified per-identity resources, external credential broker, fail-closed authorization and revocation | **Blocked** until the live capability and adversarial gates below pass |

**Do not enable or invite multi-user full-tool access based on prototype components.** Missing isolation must never cause tools to run in the credential-bearing gateway or a shared host fallback. See the [identity isolation design](./authorized-identity-isolation.md).

## Current packaging source (not a deployment record)

The current `Dockerfile.hf` application pin is `8e50e096408fdb7ab42ffc259e851732765bad8b` in the private `filewrap/hermes-agent` fork, independently of the copied helper-bundle revision. It includes locked uvloop 0.22.1 on Linux CPython and the existing five-way, 512-KiB, completion-refilled Telethon downloader with MB/MB/s output and 10-second diagnostics; `download_limits` remains removed. Record the clean helper checkout's exact `git rev-parse HEAD` separately; do not use a moving `main` or a circular self-pin. Earlier live-service versions above are historical planning context, not a claim about today's running service.

Build-time and configured-startup checks use the actual application interpreter for SQLite/FTS5 and the real gateway loop selector, requiring uvloop 0.22.1 and `uvloop.Loop` before state setup. The gateway-only selector does not replace the process-wide asyncio policy. Status-only startup and HTTP/subprocess-liveness semantics are unchanged. Use `README_HF.md` and `deploy/huggingface/README.space.md` from the reviewed helper checkout for current packaging instructions. This update does not deploy a Space or change/restart any live service.

## What HF provides—and what it does not prove

The [Docker Spaces guide](https://huggingface.co/docs/hub/en/spaces-sdks-docker) specifies a root `Dockerfile`, default `app_port: 7860`, container UID 1000, and runtime environment injection for variables and secrets. `/data` is available at runtime, not during image build. Docker GPU hardware is supported, but GPU access is not available during the build; a remote-provider smoke test does not require a GPU.

The [Spaces overview](https://huggingface.co/docs/hub/en/spaces-overview) currently requires a paid plan to create Docker or Gradio Spaces: PRO for a personal account, Team or Enterprise for an organization. CPU Basic defaults to 2 vCPU, 16 GB RAM, and 50 GB **ephemeral** disk. A paid account plan does not itself make CPU Basic always-on: free hardware sleeps when unused. Select suitable paid hardware and verify the actual Space sleep settings before relying on continuous Telegram service. Check current account, compute, and storage terms directly; this plan does not quote prices.

Choose **private**, not merely protected. Public exposes source and app; protected hides source but still exposes the running app publicly; private restricts source and app to owner/collaborators. HF privacy is not Telegram authorization: restrict Telegram identities separately. These [visibility and network rules](https://huggingface.co/docs/hub/en/spaces-overview) do not promise user namespaces, delegated cgroups, arbitrary UID switching, mount privileges, or a privileged worker supervisor.

## Preflight checklist

Complete these checks before the owner starts the test:

- [ ] Use a separate private Space, separate test bot/session, and owner-only numeric Telegram allowlist. Do not reuse the running production bot token/session: competing consumers can disrupt delivery. Do not copy the existing host's home, environment, or systemd units.
- [ ] Record the exact source commit and HF helper revision. The current repository build context is not proof that the running bot uses that revision. Pin the build source to a reviewed commit rather than a moving branch.
- [ ] Prepare the Space packaging using [README_HF.md](../../../README_HF.md): `Dockerfile.hf` becomes the Space-root `Dockerfile`, `Dockerfile.hf.dockerignore` becomes `Dockerfile.dockerignore`, and `deploy/huggingface/README.space.md` supplies root Space metadata. Retain the required HF helpers. Do this in the future Space workspace, not by replacing the personal fork's root files for this documentation change.
- [ ] Use `sdk: docker`, `app_port: 7860`, and matching `HF_PORT=7860`. Confirm the HTTP listener binds `0.0.0.0:7860`; expose no unauthenticated terminal, browser debugger, MCP administration, or arbitrary command endpoint.
- [ ] Confirm paid-plan eligibility, hardware capacity, sleep/resume behavior, and an explicit compute/storage budget. Do not assume background Telegram traffic prevents sleep.
- [ ] Choose disposable state versus recoverable snapshot-backed state below. Ensure runtime mount permissions work for UID 1000 without broadening sensitive paths to world-readable/writable access.
- [ ] Use encrypted, profile-separated snapshots and separate test data. Keep encryption keys outside the bucket and image. Directory separation is organization, not a tenant security boundary.
- [ ] Plan a coherent backup, verify a restore into empty state, and decide what data loss between snapshots is acceptable.
- [ ] Test DNS/TLS, Telegram transport, provider access, and bounded HTTP/YouTube requests from the actual HF runtime—not the current host.

### Runtime secrets and controls

Use HF **Secrets**, never public Variables, for provider keys, bot credentials, and sensitive identity information. Variables are public and propagate when a Space is duplicated; secrets do not. Both are accessible to the Docker app at runtime, so arbitrary owner tool execution can still expose them. See [HF secret handling](https://huggingface.co/docs/hub/en/spaces-overview#managing-secrets) and [Docker environment injection](https://huggingface.co/docs/hub/en/spaces-sdks-docker#secrets-and-variables-management).

| Name | Future setup |
| --- | --- |
| `OPENROUTER_API_KEY` | Secret; dedicated limited test credential, not an owner's unrestricted production key |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN` | Secrets; this fork uses Telethon/MTProto and needs the application credentials as well as the bot token |
| `TELEGRAM_ALLOWED_USERS` | Prefer Secret; only the owner's numeric ID for this trial; verify the resolved allowlist actually denies everyone else |
| `OWNER_TELEGRAM_ID` | If required by the chosen fork configuration, Secret containing the intended owner ID; verify ownership resolution rather than trusting an allowlist alone |
| `HF_STATE_MODE` | Default `unconfigured`: status service only. `ephemeral` explicitly selects disposable local trial state; `local` is only an alias, not persistence |
| `HF_ALLOW_EPHEMERAL` | Must be exactly `1` to acknowledge volatile state before either local mode can launch the gateway |
| `HERMES_HOME` | Default `/home/Hermes/.hermes`; keep within the launcher's validated local paths; do not redirect live state to `/data` or a bucket |
| `HF_PORT` | Non-sensitive Variable; `7860`, matching Space metadata |
| `HF_CONFIG_FILE` | Optional absolute path to a trusted seed config; no embedded credentials; existing/restored config takes precedence |
| `HF_RESTORE_ARCHIVE` | Optional absolute path to a trusted `.tar.gz` snapshot; restores only into absent/empty local state before startup |

Do not commit `.env`, tokens, Telethon session files, unencrypted backups, or credential-bearing config. Do not expose values in health responses, shell traces, screenshots, or logs. `HERMES_REPO_URL` and `HERMES_REF` are build arguments, not runtime deployment switches. If private source fetch needs `GITHUB_TOKEN`, use the documented BuildKit secret mechanism, never a literal token in a Dockerfile, build argument, or repository URL. Avoid handing the deployed agent a source-control write token.

## Persistence: current limitations first

[Spaces disk storage](https://huggingface.co/docs/hub/en/spaces-storage) is ephemeral and can be lost when the Space restarts or stops. HF recommends attaching Storage Buckets as runtime volumes; the mount path is configurable. The Docker documentation's `/data` convention is not permission to place Hermes live state there.

**The existing HF launcher only supports explicitly acknowledged local/ephemeral live state.** It rejects `/data` for the live home, validates supported filesystems, and does not establish bucket-backed SQLite safety. Do not bypass those checks or invent a persistent `HF_STATE_MODE` value.

For the initial trial, choose one of these recovery expectations:

1. **Disposable smoke test:** use synthetic data, accept local-state loss, and retain no important sessions or user files there.
2. **Snapshot-backed trial:** keep live state local; export closed, coherent snapshots to a private bucket or external durable store, then restore a selected trusted archive into empty local state. This provides recovery to the chosen snapshot, **not continuous durability**. The existing helpers do not install a continuous backup job. Quiesce all writers for a coherent whole-home snapshot; use SQLite backup support rather than copying a live database file blindly.

A [Storage Bucket](https://huggingface.co/docs/hub/en/storage-buckets) is mutable and non-versioned: overwrites/deletions are not a rollback history. Review bucket visibility, collaborator permissions, token scope, and current storage/access pricing. Use unique snapshot names, integrity checks, encryption, retention, and an independent backup copy. Verify whether the chosen mounted filesystem supports required locking/atomic operations before considering any future live database integration. Use read-only mounts for restore inputs where possible, and only a narrow export destination as writable.

**One shared read-write bucket mount is never tenant isolation.** Profile prefixes and encryption at rest do not stop a running shared process that can read keys or traverse other profiles. Durable multi-user state needs independently enforced access boundaries and a separately reviewed storage design.

## Staged owner-run deployment and smoke test

These are future actions for the owner, not actions performed by this documentation change. Stop at any failed gate; keep tenant tools denied.

### 1. Package and start status-only

Build the reviewed revision with the existing HF helpers. Leave `HF_STATE_MODE=unconfigured` initially. Confirm the expected non-root UID, writable local paths, and absence of production data in the image/context. The current HF image gives its UID 1000 user passwordless sudo **inside the container**; this is an additional reason to keep the prototype owner-only, not evidence of isolation or host privilege.

Check `/`, `/healthz`, and `/readyz` through the private authenticated Space and inside the runtime. `/healthz` is HTTP-service liveness and may return 200 while deliberately unconfigured. `/readyz` should return 503 until a gateway child exists. Neither endpoint proves messaging, database, or model health.

### 2. Launch the disposable owner gateway

Supply reviewed seed configuration and scoped secrets. Explicitly opt into `HF_STATE_MODE=ephemeral` and `HF_ALLOW_EPHEMERAL=1`. Confirm only the intended owner can reach tool execution and non-owner tools remain denied; do not invite tenants to test full tools. A running gateway child should make `/readyz` return 200, but require an owner end-to-end message before declaring service ready.

Use the container entrypoint and its supervised app process. The existing chain is `tini` → HF start helper → Python supervisor → `python -m hermes_cli.main gateway run`. Do not run the current Sandbox's systemd installation, copy its service unit, or assume host-level service/kernel capabilities exist in HF.

### 3. Test actual outbound connectivity

HF documents outbound ports **80, 443, and 8080**, blocking other ports. This does **not inherently block Telegram or YouTube**. Test actual transport/protocol and destinations; port reachability alone is insufficient.

- **Telegram HTTPS:** the [Bot API](https://core.telegram.org/bots/api) uses HTTPS. A small authenticated `getMe` check can test token/HTTPS connectivity if useful, but it does not validate this fork's Telethon transport. Never log the token-bearing request URL.
- **Telegram MTProto:** [official transports](https://core.telegram.org/mtproto/transports) include TCP on 80, 443, 5222 or other returned ports; HTTPS/WSS endpoints use 443. Explicitly configure and verify the selected client's MTProto connection on **443** when alternate ports are blocked. Inspect actual destination ports, including reconnects/DC migration/media transfers. TCP MTProto on port 443 is not automatically HTTPS. Do not invent a port environment variable: use a supported client configuration path; if the current build cannot pin/test it, record a blocker for a separate change rather than claiming connectivity is solved.
- **End to end:** send one owner message, receive a model-backed reply, upload/download one small synthetic attachment, disconnect/reconnect, and verify no duplicate side effects. Do not switch the live production bot's polling/webhook/session settings.
- **OpenRouter:** perform one bounded request using the selected test/free-only route and intended policy; verify TLS, model routing, response, timeouts, and redacted failure handling. A provider request is not proof of a deployed credential broker.
- **HTTP/YouTube:** fetch a harmless HTTP/HTTPS URL and a small permitted YouTube metadata/sample operation. Test redirects and download-host connections with size/time limits. Distinguish network failures from upstream authentication, rate limits, content restrictions, or tool dependencies; do not bypass access controls.

### 4. Exercise lifecycle and recovery

Record selected commit, hardware, state mode, and redacted results. Test graceful stop, gateway-child exit, restart/rebuild, and pause/resume where available. The existing supervisor treats an unexpected gateway exit, even exit code zero, as a deployment failure; verify it does not leave a misleading healthy front door. Confirm process-group teardown leaves no orphan jobs.

Write synthetic state, create and verify a coherent snapshot, then restart and restore into an empty home. Compare expected conversation/settings/sample-file checksums and verify reconnect without duplicate work. Expect unsnapshotted local changes to be lost. Check that deleted/revoked test credentials are not silently recovered from old config/session backups. Never merge a restore over nonempty live state.

**Smoke-test pass:** private owner-only access, correct health semantics, real Telegram/OpenRouter/HTTP success, graceful shutdown, understood sleep/restart behavior, and a demonstrated restore or explicit disposable-data acceptance. This pass authorizes only continued single-owner testing.

## Separate worker-capability gate: no assumptions about HF

Run these probes only in an owner-controlled disposable runtime with synthetic identities and canary data. Record observed kernel enforcement and denials, not just configuration strings or unit-test results. Do not request privileged mode or weaken controls to make a probe appear to pass.

| Boundary | Required live evidence before multi-user tools |
| --- | --- |
| User namespaces and identities | Actual namespace creation and UID/GID maps; distinct unprivileged identity allocation, effective UID/GID switching where required, no privileged supplementary groups, no ambient capabilities, and `no_new_privs`. Demonstrate no cross-identity process/file access. Container UID 1000 alone is not enough. |
| Delegated cgroups | An actually writable, delegated cgroup subtree; placement before worker code executes; enforced memory/CPU/PID limits; descendant accounting and complete kill on cancellation/revocation. Merely reading `/sys/fs/cgroup` proves nothing. |
| Filesystem/process mounts | Private mount/PID/IPC boundaries, clean read-only runtime, only the current identity's writable data, constrained `/proc`, no other homes/profiles, `.git`, host sockets, credentials, or writable host mounts; reject traversal and symlink escape. Verify actual denied reads/writes. |
| Network egress | Per-worker default-deny enforcement, identity-bound allowed destinations, DNS/redirect/rebinding handling, metadata/loopback/private-network blocking as applicable, and no route around the broker. HF's port filter alone is not an identity policy. |
| Browser and MCP resources | Worker-owned browser processes/profiles/cookies/debug sockets and MCP connections/tokens; no shared credential-bearing integration; identity-scoped downloads, child jobs, and teardown. Demonstrate A cannot use B's resources. |
| Credential broker | Separately provisioned trusted broker retains provider keys; workers receive no raw owner key in environment, files, process state, or logs. Use short-lived identity/authorization-generation-bound capabilities, narrow inference RPC, free-only route and zero-price policy where required, quotas, cancellation/revocation, replay rejection, and no arbitrary URL proxy. |
| Identity lifecycle | Trusted caller identity, per-action/continuation authorization checks, lease expiry, no stale capability/UID reuse, cleanup of queued/running/background/scheduled work, and deny-on-broker/supervisor failure with **no host fallback**. |

At this baseline, `gateway/isolated_worker/` is unenabled prototype code; its production readiness gate is deliberately false. Launch specifications, synthetic tests, and destination-pinning logic are not evidence of provisioned workers, kernel isolation, or a running broker. There is no environment switch that turns the prototype into a production boundary. Follow the [full isolation release gate](./authorized-identity-isolation.md), not an imagined enable flag.

**If any required boundary is unavailable, fails, or remains unverified: stay single-owner, keep non-owner tenant tools denied, and do not execute those tools in the gateway.** If HF cannot provide the required primitives, evaluate an independently provisioned worker/broker environment in a separate project; do not reinterpret the HF container as a substitute.

## Acceptance criteria for any multi-user promotion

Promotion requires all of the following, not merely a successful Docker build or Telegram reply:

- A reviewed and explicitly approved supervisor/worker/broker deployment, with measured HF runtime capabilities or separately isolated execution infrastructure; every boundary above passes on the actual target.
- A whole agent and all tool execution stay outside the credential-bearing gateway. Provider credentials remain only with the trusted broker; workers receive only scoped, revocable capabilities.
- Live **A → B → A** synthetic-canary tests show no leakage across filesystem, environment, `/proc`, provider access, browser cookies, MCP resources, network, logs, attachments, concurrent sessions, child/background/scheduled jobs, and restore paths.
- Negative/escape tests cover forged identity, traversal/symlinks, stale leases, revoked authorization, reused resources, direct egress, broker/supervisor outage, cancellation races, process descendants, and resource exhaustion. Failure is denial, never host execution.
- Restart/resume/rollback preserves isolation and does not resurrect revoked capabilities or credentials. Quotas, telemetry redaction, incident response, backups, and restore drills have owner-approved operating procedures.
- The owner explicitly approves promotion after reviewing the live evidence. A separate reviewed implementation/release change is required; this plan grants no multi-user release approval.

## Rollback and recovery

On a failed test, stop/pause only the experimental HF workload, deny test ingress, and revoke its dedicated test keys/session if exposure is suspected. Keep the existing `22d2361` service and production bot untouched. Do not solve an HF failure by restarting the existing gateway or reusing its credentials.

Record the failing commit and redacted diagnostics. Rebuild from a known-good pinned application/helper pair, using compatible configuration and schema; restore a verified encrypted snapshot into empty local state and repeat owner-only acceptance checks. A Git rollback does not undo bucket mutations or database migrations. Preserve a compatible pre-change snapshot and independent backup; do not downgrade a database blindly. No successful restore or health check relaxes the multi-user gate.
