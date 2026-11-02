# Full tools for authorized identities: worker components remain unenabled

## Decision

Do **not** remove the tenant tool firewall. Globally `/allow`-authorized people should have the full Hermes toolset in their own identity, but the current gateway cannot safely provide that. This change hardens the restricted runtime; it does **not** implement or announce full-tool support.

The blocker is architectural, not missing Linux primitives: a minimal `bwrap --unshare-all` process and an unprivileged user namespace both run on the deployment host. Neither is currently the execution boundary for a tenant agent.

## Why profile directories are insufficient

`gateway/tenant_runtime.py::_make_agent` constructs the agent **inside the gateway process**, with the gateway's OS identity, and passes a resolved provider API credential into its client. `_resolve_owner_provider` is a trusted credential-resolution phase, not a credential-hiding inference proxy. Setting a profile home and an empty secret scope only changes cooperating readers. It does not prevent arbitrary shell/Python code from opening absolute host paths, reading process state, using inherited credentials, or connecting to host-local services.

The profile cache is beneath the owner's home. Different subdirectories do not provide an OS access-control boundary when execution uses the same host UID. A terminal-only container backend would not be sufficient: inline memory/session tools, browser clients, MCP/connectors, delegation, cron, outbound adapters and process-management code can still execute in the gateway or reuse its clients. Restricting tool names or checking file-path strings cannot make unrestricted Python/shell execution safe.

The previous top-level `_execute_tool_calls` denial also did not protect raw registry dispatch, direct inline invocation, independently invoked sequential/concurrent executors, or warm schema caches. The hardening now denies those supported dispatcher doors before handlers, hooks, middleware or catalog probes run. Production tenant agents retain their frozen `TenantScope` as a deny-only marker, so a lost callback/thread context does not silently grant those agent-level execution paths owner privileges.

These checks are a **restricted-runtime firewall**, not a sandbox. A background operation that loses both its context and its owning agent has no trustworthy tenant identity in the current process. No host tool is enabled on the strength of these checks.

## Audited hardening, not full-worker support

Tenant turns now capture a server-owned authorization stamp before waiting for their room lock. The stamp binds the immutable transport user, chat type, chat ID and forum thread to the durable identity and group-settings generations. Every explicit identity grant/revoke/ban/unban and scoped privilege change advances the identity generation; ordinary profile/start metadata does not. Group settings advance their own generation. Legacy records start at zero, and corrupt/missing generation APIs fail closed.

Guest chat-policy writes rotate a separate opaque revision in the exact group's config record, so `/allowall OFF` followed by `ON` cannot revive an older guest turn. Only guests depend on this guest-policy revision; globally authorized people retain their independent admission. This does not change the existing authorization-before-trigger rules or grant any new guest capability.

The restricted runtime rechecks admission after the room lock, provider resolution, model execution and asynchronous flush, and before persistence/accounting/return. Completed duplicate receipts carry their original stamp; a receipt from an earlier grant or different thread cannot be replayed. Legacy unstamped receipts are rejected rather than replayed or silently reinferred. A stale or uncertain turn may require a new message ID after access is restored.

Disabled-group responses are decided before creating a pending receipt or resolving provider credentials. Repeating such a message does not become an uncertain inference retry. Regranting access deliberately preserves the identity's already-persisted profile, memory and conversation history; it invalidates old in-flight work and response receipts, not ownership of prior data. No cross-identity history access is introduced.

These checks discard stale continuations at the checked boundaries. They do **not** cancel an already-running in-process provider call, kill descendants, create a distributed transaction spanning authorization and all data writes, or roll back history/accounting that preceded a later flush-time revocation. A supervisor-owned worker and generation-checked broker/storage operations are still required for those guarantees.

The deployment hardening drop-in is `scripts/systemd/hermes-personal-gateway.service.d/10-no-host-privileges.conf`. It adds `NoNewPrivileges=yes`, an empty capability bounding/ambient set, and `RestrictSUIDSGID=yes` to the **existing** service without replacing its executable, environment, user or lifecycle. This prevents a gateway descendant from gaining host root through sudo/setuid/file capabilities; it is not filesystem, network, browser or tenant isolation. A disposable systemd scope verified sudo denial and namespace/file-operation compatibility before deployment. Do not remove this guard merely to make future workers launch; provision their privileged supervisor separately.

## Current deployment blocker

The host can create unprivileged mount/PID/network namespaces, including under the privilege guard. It is not accurate to claim that Linux sandboxing is unavailable. However, the running gateway has `Delegate=no`; its `cgroup.procs`, `cgroup.subtree_control` and `cgroup.kill` are root-owned and not writable by the gateway UID. There is no provisioned identity-worker supervisor/delegated subtree, clean runtime image, destination-controlled network egress service, or credential-free inference RPC. Existing browser/MCP/job clients are still gateway-side process/network clients, not worker-owned resources.

A service-manager/privileged provisioning stage must first create an unprivileged worker security identity and supervised per-identity cgroups with teardown and resource limits. Then implement the narrow free-only inference broker, private worker volumes/runtime/network, and worker-owned browser/MCP/delegation/scheduler/outbound resources. These are concrete missing production components, not a reason to enable host tools or use a terminal-only wrapper. No isolated worker was shipped or enabled by this hardening, and no full-tool release gate has passed.

## Required implementation boundary

1. **Trusted ingress and authorization.** Keep the existing Telegram transport/control service as the only ingress. Resolve immutable platform user ID, group/forum scope, authorization class and policy generation server-side. Never accept an identity, owner flag, profile path or authorization class from the model. Recheck current authorization when admitting each tool action and each asynchronous continuation. Cancellation/revocation must invalidate queued capabilities and terminate the affected identity's workers and descendant jobs.

2. **An isolated whole-agent worker per identity.** Launch the entire agent and its tools outside the credential-bearing gateway process, with mount, PID, IPC and network separation. Give it only a clean, read-only runtime and that identity's writable profile/workspace. Do not mount the owner's home, other profiles, repository `.git`, host `/proc`, browser sockets, cloud metadata, host runtime sockets or service credentials. Use a dedicated unprivileged host security identity, no supplementary privileged groups, no host sudo, no ambient capabilities and `no_new_privs`. Pin limits and process teardown to the worker's cgroup. Do not count user-namespace root as permission to operate on the host.

3. **A credential-free inference broker.** Retain the owner's provider credential only in a trusted external broker. Give the worker a short-lived, identity-bound capability to a narrow inference RPC, not the provider credential. Enforce the configured free-only route (`openrouter/free`), zero-price provider policy, limits and authorization generation at the broker. Do not make an arbitrary URL-forwarding proxy. Broker capabilities must not become bearer credentials usable by another identity.

4. **Private network and browser resources.** Run browser processes and authenticated profiles inside the identity's worker boundary. Do not attach to the owner's existing browser or a shared remote-debugging endpoint. Provide controlled outbound web access without routes to the host, sibling workers, private service endpoints or metadata. Enforce destination rules after DNS resolution and on redirects, including IPv6; merely stripping environment variables or naming a profile is insufficient. Network access must not reintroduce a route to shared authenticated browser or connector services.

5. **Identity-owned MCP/connectors and outbound policy.** Discover and instantiate only the calling identity's configured servers, credentials and authenticated sessions inside its boundary. Put any shared connector service behind an identity-scoped authorization broker; never reuse owner connector clients. Outbound messaging must use an explicit destination capability and current policy checks, rather than exposing a bot/session credential or unrestricted gateway callback to worker code.

6. **Delegation, jobs and persistence stay inside that boundary.** Children, inline/search-resolved calls, background shells, schedulers and completion callbacks inherit the immutable identity and policy generation, not just a session-name prefix. Store memory, task state, approval grants, skills, session databases, process registries and browser data in the owning identity's volume. A trusted scheduler may launch workers, but must not run tenant Python in the gateway. Return results through identity-bound receipts; do not replay stale callbacks after a ban, revoke or policy change.

7. **Discovery and execution share policy.** A full-worker authorization grant is distinct from guest capabilities. Filter discovery against the current identity and check again immediately before execution, including connector batches, search-resolved tools, inline executors, delegated children and resumed jobs. Every unknown/missing identity fails closed. Container/worker startup failure must not fall back to the local host backend.

## Permission contract to preserve

- Owner and global `/allow` identities are authorized people wherever they communicate; their data and execution identities remain separate. A group guest policy must not downgrade a globally authorized person. Group/forum conversation visibility must not cause one member's private DM memory to be loaded into another member's turn.
- An unlisted identity has no DM access. `/allowall` grants only the specific group's unlisted members scoped access, not a global identity grant. `/gsettings` guest capabilities apply only to those unlisted group guests.
- Authorization is checked before invocation triggers. Mentions, replies and names never authorize an identity. `/allowall OFF` disables name invocation even for the owner or globally authorized people, while authorized `/gsettings` remains usable.
- Bans, revocations, forged/stale callback identity and forum-thread changes are checked against current trusted transport state before a continuation is admitted.
- Preserve the existing model/settings/approval/research/skills/attachment/menu behavior and service ownership. Do not introduce a second Telegram consumer or terminal-owned gateway.

## Release gate for full-tool support

Before enabling tools, run a live isolated-worker A → B → A test with separate identities and synthetic canaries for files, `.env`, memory, tasks, browser cookies and processes. Exercise absolute paths, traversal, symlinks, `/proc`, environment reads, process signaling, local/metadata network access, browser attachment and connector credential reuse. Include real shell/file/browser/MCP and inline/search/concurrent paths, child agents, background completions, schedules and outbound attempts. Prove the owner still works, guests remain constrained, revoked workers stop, callbacks cannot cross identity or policy generation, no host sudo exists, and worker failure cannot choose a host fallback.

The current regression tests prove denial and existing data-routing contracts only. They are not evidence that this full-worker release gate has passed.

## Unenabled code components

`gateway/isolated_worker/` now contains code for the future external boundary. It
does not change the Telegram consumer, gateway service, existing owner routing,
`TenantScope`, or the empty tenant tool allowlist. `production_ready()` is
deliberately false. There is no configuration or environment switch that enables
these components inside the shared gateway.

- `admission.py` captures trusted transport identity and existing durable
  authorization stamps, and separately verifies a **global** grant. A scoped
  group-admin grant cannot masquerade as global authorization. Owner/global
  identities remain independent of guest capabilities; guests cannot enter this
  full-worker path. Existing authorization-before-trigger handling is unchanged.
- `supervisor.py` defines a bounded, kernel-peer-checked Unix control client and
  immutable launch specifications: clean read-only runtime, dedicated host UID,
  private namespaces and identity volume, room/thread-owned resources, sanitized
  environment, no direct network, cgroup limits and mandatory descendant teardown.
  A specification is **not** a sandbox and a supervisor's JSON response is **not**
  kernel attestation. There is deliberately no gateway-side process launcher.
- `lifecycle.py` supplies the external supervisor's admission/revocation state
  machine and a real cgroup-v2 limits/kill/readback adapter. It refuses a fake
  filesystem or symlinked delegated root, quarantines uncertain teardown, binds
  host security UIDs permanently to an identity for the controller lifetime, and
  bounds controller state. It does not create service accounts or delegation.
  The future launcher must attach a child **before** tenant code executes;
  `Popen` followed by moving a live PID is not an acceptable substitute.
- `inference.py` retains provider credentials only in the trusted fixed-origin
  transport. Workers receive short-lived grants bound to exact attested workers
  and authorization stamps. The broker rejects route/header/provider overrides,
  enforces `openrouter/free`, zero prompt/completion prices and no provider
  fallback, and rechecks authority around inference. Streaming and multimodal
  requests are intentionally unsupported rather than forwarded unsafely.
- `rpc.py` implements an actual private inherited Unix `SOCK_SEQPACKET` inference
  channel. Requests carry kernel `SCM_CREDENTIALS`; the trusted supervisor must
  verify sender PID/UID/GID against its live worker/cgroup registry. Worker JSON
  cannot select a peer, identity, URL or arbitrary gateway method. Merely checking
  `SO_PEERCRED` on a socketpair would authenticate its creator, not its child.
- `worker.py` supplies a supervisor-internal whole-`AIAgent` factory and fixed-binding
  turn protocol, with no CLI, transport consumer or gateway-side launch path. It
  checks the exact clean environment and private resource directories and replaces
  agent client creation/refresh with the credential-free inference facade. These
  Python checks do not attest mount/PID/network isolation or make an ordinary host
  process safe. The external launcher and live release gate remain mandatory.
- `egress.py` validates public HTTP(S) destinations, pins resolved addresses while
  retaining TLS hostname verification, and revalidates redirect destinations and
  authorization. It is a bounded fetch transport, **not** a network namespace,
  general proxy, browser bridge, or complete authenticated connector transport.
- `jobs.py` binds inline/child/background/scheduled continuations and one-use
  receipts to exact worker, identity, room/thread and generation. Outbound text
  requires a bounded exact-destination capability; no bot credential or general
  adapter callback is given to a worker. State is in-memory, so restart invalidates
  handles; this is not yet a durable production scheduler. An already-started
  external send cannot be rolled back by revoking its eventual receipt.

Synthetic tests cover controller A → B → A separation, trusted durable-store
admission, hostile path-like identity strings, private resource specifications,
real Unix control/inference sockets and kernel sender credentials, cross-peer
grant theft, free-only routing, stale authorization and callbacks, delegated jobs,
outbound destinations, quotas, and simulated teardown/resource failures. Egress
tests use local HTTP fixtures with synthetic DNS/dialers. Worker-bootstrap tests
replace the expensive agent initializer; they do not exercise a real full-tool
worker turn. Existing real-agent regressions exercise only the restricted
runtime, not a full-tool worker. None launches a production isolated
worker or proves arbitrary shell/Python confinement. Do not describe these tests
as the live release gate.

## Host changes requiring new approval — not applied

The existing `hermes-personal-gateway.service` still has `Delegate=no`, runs as the
owner's host UID, and retains `NoNewPrivileges=yes`. Do not change it to delegate
host privileges or remove its firewall. The following is the concrete proposed
provisioning boundary, not an instruction that has been executed:

1. Create a **separate** system-level `hermes-worker-supervisor.service`, a
   root-owned bounded control socket at `/run/hermes-workers/control.sock`
   (mode `0660`, accessible only to the gateway client group), and a root-owned
   immutable identity-to-host-UID registry. Provision a distinct non-login host
   UID/GID for every identity, with no supplementary groups or sudo. The supervisor
   needs narrowly constrained permission to launch processes as those UIDs and
   create/chown their private resources; the gateway and workers must not receive
   that permission. A root supervisor would require explicit approval for
   `User=root` and `CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN`, with
   `NoNewPrivileges=yes`, `ProtectHome=yes`, `ProtectSystem=strict`, and private
   devices/tmp. Writable paths would be limited to `/var/lib/hermes-workers`,
   `/run/hermes-workers`, and that service's delegated cgroup subtree; all tenant
   processes must drop the supervisor capabilities before execution. No
   `CAP_SYS_ADMIN`, `CAP_SYS_PTRACE`, arbitrary host command RPC,
   setuid helper, or tenant sudo is part of this proposal. The actual launcher and
   confinement profile must still be implemented and audited before installation.
2. Delegate **only that supervisor's** cgroup-v2 subtree with
   `Delegate=cpu memory pids`. Before child execution, enforce `memory.max=1GiB`,
   `memory.swap.max=0`, `pids.max=128`, `cpu.max="100000 100000"`, a 300-second
   supervised wall deadline, and working `cgroup.kill` plus populated-state
   readback. Revocation, disconnect and deadline must invalidate broker/job grants,
   kill every descendant and prove the cgroup empty. Startup must fail if atomic
   pre-execution cgroup placement or any controller is unavailable.
3. Provision a clean verified runtime image under a root-owned
   `/opt/hermes-workers/runtime/` and identity-owned volumes under
   `/var/lib/hermes-workers/identities/<identity-sha256>/` (mode `0700`, stable
   exclusive UID ownership). Enforce a **hard 1GiB per-identity storage quota**
   using an approved quota-capable backing store; ordinary directories are not a
   quota. Mount only the read-only image and the current private volume into the
   worker. No owner home, repository `.git`, host `/proc`, bus/runtime/browser
   sockets, sibling volumes or gateway credentials may be reachable. The current
   launch specification describes these invariants but does not implement them.
4. Provision `hermes-inference-broker.service` under a separate non-login account,
   with the provider key supplied only to that service through a service-managed
   credential. Its public egress is restricted to the fixed OpenRouter inference
   endpoint; workers use inherited peer-attested RPC channels, never its key.
   This requires explicit approval to make the existing owner key available to
   that separate trusted service. No credential is to be copied to worker files,
   environment, arguments, browser profiles or connectors.
5. Provision `hermes-worker-egress.service` under another non-login account with
   no inference/gateway credentials. Worker network namespaces have no direct host,
   sibling, DNS, internet or metadata routes; only a separately enforced
   destination-checked channel may reach this service. Register the host's public
   addresses and shared control/browser/MCP/connector endpoints as protected
   destinations as well as private/metadata ranges. Browser proxy integration and
   identity-owned authenticated MCP/connectors still need concrete transports;
   a GET/HEAD fetch helper alone cannot enable the whole Hermes toolset.

Approval to provision those host components is necessary but not sufficient to
enable tools. Remaining implementation includes the actual whole-process
namespace/image launcher, live registry/cgroup sender attestation, authenticated
browser/connector and durable scheduler integration, deployment wiring and the
full live canary/escape test matrix above. Absolute path/traversal/symlink escapes,
environment/process inspection and signaling, unrestricted inline/concurrent/
child/background tools, browser cookies and MCP credentials must be attacked
inside real workers before support can be claimed. Until then, keep tenant tools
denied and leave the existing service running unchanged.

## Validation of these components

The combined run passed **247 tests**: `tests/gateway/test_isolated_worker_*.py`
plus the existing local-tenant, Telegram access-role/personal-skill, authorization
-generation, tenant tool-boundary/runtime/production/real-agent/continuation,
Telegram authorization/callback, and group-gating regressions. Python compilation
and `git diff --check` also passed. A combined-run import-order issue in the
synthetic bootstrap fixture was fixed without changing production error handling
or deployed runtime code. No live provider request, deployment worker, host
provisioning, service restart or second Telegram consumer was used for validation.
The live release gate remains unpassed.
