# Local Telegram access, tools and audit logging

This change separates conversation admission, owner-managed tool capabilities, and private audit delivery. Transcript auditing is strictly private-DM-only: no group, supergroup, channel, or forum message is transcript-audited, including messages from the owner or explicitly allowed users. It does not introduce a second Telegram client, remote worker, or deployment action. The configured owner continues through the ordinary unrestricted runtime; tenant policy does not constrain the owner.

## Implementation plan and delivered boundary

1. Keep non-allowed identities blocked and keep group `Allow all` as conversation-only guest admission in that group. Preserve `/allow` and `/disallow` checks and confirmations. Manage the roster and policies separately with owner-private `/allowlist`.
2. Give explicitly allowed local users useful tools, but enforce policy before tool execution, including direct registry/model calls, inline calls and agent-bound callbacks. Use real Linux namespaces rather than treating profiles, working directories, or ContextVars as security boundaries.
3. Partition tenant sessions, receipts, histories and workspace locks by profile, numeric user ID, chat and topic. Refuse unverifiable reply anchors instead of falling back into General. Report topic creation only after a valid Telegram creation response.
4. Keep audit delivery local and separate transcript from operational events: only authorized private-DM transcripts may enter the durable outbox or create a per-user topic. Group, supergroup, channel, and forum messages must not enqueue transcript records or cause transcript-topic creation, regardless of sender, allowlist, owner status, trigger/mention, or Telegram topic. Trusted startup notices and sanitized Operations events remain supported. Multi-profile routing through one adapter is permitted only against the live served profile registry and pinned identity; unverifiable or mismatched identities fail closed. Validate the private forum and Telegram rights before enabling it.
5. Exercise these paths offline and test the local namespace against harmless private canaries. Commit/push source changes separately from any restart. Live Telegram permissions and delivery require owner configuration and are not proven by offline tests.

## Owner commands

Use `/allowlist` in the configured owner's private chat. Pages show stable numeric IDs and the effective role/user policy. Existing `/allow <numeric_id>` and `/disallow <numeric_id>` retain their own confirmation and access semantics. Group settings no longer display the user roster or per-user revoke buttons. Group guests do not gain global admission, DM access, tools or administrative permissions.

View the default allowed role:

```text
/allowlist policy allowed
```

Set a read-only default:

```text
/allowlist policy allowed {"permitted_effects":["read"]}
```

Give a particular allowed user bounded file access with a denied resource:

```text
/allowlist policy 123456789 {"allowed_tools":["read_file","write_file"],"denied_paths":["/workspace/private"]}
```

Restore the default three bounded capabilities for that user:

```text
/allowlist policy 123456789 {"allowed_tools":["terminal","read_file","write_file"],"permitted_effects":["read","write","shell"],"allowed_paths":["/workspace"],"denied_paths":[],"denied_actions":[],"denied_tools":[]}
```

The owner-controlled `tenant-tool-policies.json` lives in that profile's home. Subjects are `allowed` or positive numeric Telegram user IDs. A user's explicitly supplied fields replace the corresponding default-role fields; omitted fields inherit. Changes are enforced at the next tool call, even if an old schema was already cached. They do not cancel a command that has already started (maximum wall time: 60 seconds).

Supported string-array keys:

| Key | Meaning |
| --- | --- |
| `allowed_tools` | Supported adapters: `terminal`, `read_file`, `write_file` |
| `denied_tools` | Deny a named adapter; not a ban on equivalent shell behavior |
| `denied_actions` | Deny a tool/action name or `read`, `write`, `shell`; any action restriction suppresses arbitrary shell |
| `allowed_paths` | Canonical virtual paths under `/workspace` |
| `denied_paths` | Denied canonical resources under `/workspace`, including symlink targets and file hard-link aliases |
| `permitted_effects` | `read`, `write`, `shell` only |

A deny on `read_file` alone does not prohibit reading through an explicitly permitted shell. Use `permitted_effects`, `denied_actions` or resource restrictions to constrain the behavior itself. Arbitrary shell is exposed only when the entire workspace has read/write permission, no resource/action restriction is present, and the shell effect is permitted. Structured file tools remain available under narrower policies.

## What the local sandbox actually does

The ordinary `LocalEnvironment` backend is **not** a sandbox: a harmless real test read a sibling file outside its configured working directory. It is never used as the allowed-user security boundary.

The supported tenant adapters instead launch local Bubblewrap with separate user, mount, PID, network, IPC and UTS namespaces, no retained capabilities, no nested user namespaces and a cleared environment. The profile's dedicated room workspace is mounted as `/workspace`. Host homes, owner configuration, owner credentials and other tenant workspaces are not mounted. Standard system binaries/libraries are mounted read-only. There is no network access, and no host-execution fallback when the kernel or Bubblewrap is unavailable. File operations resolve and enforce policy inside that same namespace.

Prerequisites are Linux, usable unprivileged user namespaces, `bwrap`, `/usr/bin/prlimit` and `/usr/bin/python3`. A successful trivial namespace probe does not certify another machine or an HF runtime. Missing prerequisites leave execution explicitly unavailable; they never switch to the ordinary local backend.

The subprocess receives a 60-second maximum wall timeout, 60 CPU seconds, 256 MiB per-process address-space limit, 128 open files, an 8 MiB **per-file** size limit, a configured process-count rlimit and a 16 MiB `/tmp`. Returned output is capped at 100,000 characters. PID-namespace teardown prevents detached children from continuing after the command exits.

**Important remaining limitation:** these are not per-tenant aggregate CPU, memory, process or persistent-workspace disk quotas. In particular, many individually small files can fill the host filesystem. The process-count rlimit is per real UID, not an independent tenant budget. This stage does not claim protection against every resource-exhaustion attack or kernel exploit. Owners sharing shell access with hostile users still need a quota-backed local execution boundary before treating host availability as protected.

## Explicitly unsupported in this stage

Allowed users do not receive arbitrary host tools, browser sessions, MCP tools, uploads/publishing, external network access, delegation, background/deferred tools, or alternate terminal backends. Incoming attachment caches are not automatically mounted into tenant workspaces. A requested unsupported tool is denied at execution and reported through Operations when audit logging is configured, otherwise through the local operational log. No denial automatically bans a user.

The agent's frozen tenant binding protects callbacks that lose ContextVars. There are no permitted detached/delegated tenant-tool routes in this stage; a future new callback route must carry that binding rather than invoking an unscoped ordinary-owner dispatcher.

## Private forum: DM-only transcript audit

The transcript-audit boundary is **private direct messages only**. A message from a group, supergroup, channel, or forum is never copied as a transcript, even if it is authored by the owner or an explicitly allowed user, the sender is otherwise authorized, the group allows all guests, the message mentions/triggers the bot, or it belongs to a forum topic/thread. Such messages must not be enqueued as transcript events, placed in or replayed from the transcript outbox, forwarded, or cause creation/resolution of a per-user transcript topic. Conversation handling in a group is independent of audit eligibility. Pending legacy group-scope transcript rows must be discarded, not delivered.

Authorized private-DM transcripts remain eligible for the existing private-forum audit path. Trusted startup/deployment notices may still go to General, and sanitized operational warnings/errors, policy denials, and delivery failures may still go to the per-profile Operations topic; these are operational events, not group transcript copies. Setup may create/verify the General and Operations topics, and an eligible private DM may create/reuse its user topic. No group-originated transcript may trigger a topic operation. Operations contains no user-message content or raw credential-bearing exception values. No deployment is performed by this change.

After a separately approved restart, use `/logs setup <channel_id>` (a raw positive channel ID or canonical negative `-100…` ID) and then `/logs status` in the owner's private chat for **each selected routed profile**. This configures and reports only the profile selected by the live served registry; setup/status must be repeated for every profile that is meant to log. `/logs off` disables it. Bare `/logs` retains the existing private local-log viewer. Settings are stored in that profile's `platforms.telegram.extra.forum_audit` configuration. No live forum verification or completed setup is claimed here.

A positive ID is resolved explicitly as a Telegram Channel peer, never as a user. Setup verifies that the returned Channel matches the requested ID and is a private forum before normalizing and storing its canonical negative peer ID. User entities, wrong channels, public groups and non-forums fail without enabling or replacing the destination. Use a private Telegram forum, not a public group. Naina must be an administrator with **Manage Topics**; setup checks the live bot participant's native Telethon admin rights rather than trusting saved verification flags. General is identified by Telegram topic ID 1, including when renamed, and must be open/visible. General accepts only trusted startup/deployment notice keys. Operations is created or verified separately for every profile, named `Operations · Profile <canonical hash>`, so profiles can share one forum without mixing streams.

Bot setup and recovery use `messages.GetForumTopicsByIDRequest`, never the bot-unsupported full topic-list request. General is checked by ID 1; Operations and eligible private-DM user-topic IDs are reused from profile configuration or destination-scoped durable records and verified by ID. A missing eligible DM mapping may create a topic with a stable MTProto random ID; the exact ID is correlated with the request's random-ID mapping and matching peer/title topic-create service message before the subsequent by-ID check. Failed/ambiguous requests do not count as successful setup. Invalid saved IDs fail closed rather than creating replacements. Existing unrecorded topics are not discovered by title enumeration.

Each profile has its own destination mapping, durable outbox and identity binding. Multi-profile routing through one adapter requires the live served registry and pinned profile identity; missing, stale, ambiguous or mismatched identity fails closed, without falling back to another profile. Copied profiles or changed destinations cannot inherit another profile's private pending events or routes. Revoked users' pending DM events are denied when drained. Incoming messages in the audit forum itself are excluded before conversational processing, so they are neither discussed by the agent nor recursively audited. Protected content is not downloaded or re-uploaded just for logging; if a permitted private-DM forward is unsupported, a content-free omission notice may be sent only to that DM sender's own audit topic. No DM delivery failure is rerouted to General or another user's topic.

The bounded best-effort outbox, dedupe receipts, deterministic retry IDs, FloodWait handling and profile/receiving-transport isolation apply only to permitted private-DM transcripts and operational events; they are not an unlimited or exactly-once archive. Queue/quota exhaustion or abandoned delivery is reported locally and, when possible, to that profile's Operations topic. Operational log capture requires explicit profile context; unscoped process warnings stay local rather than being attributed to the launch profile. This is an in-process gateway design, not a cross-process lease: do not run concurrent gateway processes over the same profile audit store.

Ingress requires private chat scope and a matching native Telegram user peer. A valid thread within an actual private DM remains eligible; a thread ID alone never determines chat type or grants audit access. The drainer checks the resolved peer again before any user-topic work and before forwarding. On opening an audit outbox, pending non-DM, malformed-scope or unknown-scope transcript rows and their pending dedupe receipts are removed, even during FloodWait or when retry dates are in the future. Cleanup emits only a generic local diagnostic, never group content or source identifiers in Operations. Already delivered Telegram messages are not deleted by this change.

Offline checks cannot verify the owner's real forum, bot rights or native Telegram delivery. Existing private-forum configuration is preserved. After a separately approved gateway restart, review `/logs status` in the owner's private chat for every selected routed profile; live verification is not claimed complete here. The namespace proof applies only to this local Linux host; an HF host still has to pass its own probe, otherwise tenant execution stays unavailable. These source/documentation changes themselves do not restart the gateway, create another Telegram consumer, or deploy/change an HF Space.
