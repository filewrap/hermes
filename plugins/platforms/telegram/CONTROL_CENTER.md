# Telegram control centre

The Telethon adapter provides owner-only setup and service controls without adding another agent, browser backend, or configuration store. Open `/setup` in a private chat. Its model, provider, personal-memory, skills, and diagnostics links delegate to the existing Hermes controllers and gateway; conversation requests still use the normal agent and tool executor.

## Commands

| Command | What it does |
| --- | --- |
| `/setup` | Opens the configuration hub. |
| `/setup terminal` | Edits the profile's working directory and command timeout. |
| `/setup compression` | Edits compression enabled/threshold/target ratio/protected recent messages. |
| `/setup display` | Edits reasoning and interim-message display settings. |
| `/browser` or `/browser status` | Shows effective browser settings and profile-scoped active sessions. |
| `/browser connect [local-endpoint]` | Checks an already-running loopback CDP browser, then saves this profile's `browser.cdp_url`. The default endpoint is `http://localhost:9222`. |
| `/browser disconnect` | Clears only this profile's CDP override; it does not terminate unrelated browsers or change the selected provider. |
| `/dashboard` or `/webui` | Opens the standalone service controls. These are aliases for the existing Dashboard/WebUI service, not two servers. |
| `/dashboard status`, `/dashboard start`, `/dashboard stop` | Inspect, start, or stop only the owning profile's existing standalone service. The same operations work with `/webui`. |
| `/dashboard theme [name]` | Lists existing themes or saves a selected theme. Also works with `/webui`. |
| `/webauth`, `/dashboard auth`, or `/webui auth` | Opens the profile's existing username/password login-provider setup. |
| `/thread help` | Shows the supported native Telegram topic operations. |

Setup writes use the shared configuration APIs, then read back through the effective runtime configuration loader. Configuration and typed-input state are bound to the originating profile, home, owner, chat, and topic. A new command cancels a pending setup input instead of being saved as a field value. Some changes affect future agent turns or require the relevant service to restart; the cards state that rather than promising an immediate live reload.

## Browser and standalone boundaries

Browser controls do not select a different provider, change credentials or free-only policy, add stealth features, or launch an OS browser. A local debug browser must already be running. Process-level `BROWSER_CDP_URL` overrides and Browser Use mode are respected rather than overwritten. CDP URLs and credentials are not printed in cards. Remote, credential-bearing, and ambiguous endpoints are rejected.

The standalone start command invokes the existing CLI with an explicit profile, `--isolated`, `--host 127.0.0.1`, port `9119`, and `--no-open`. It never supplies `--insecure`. A profile-scoped child environment prevents launch-profile Dashboard credentials from leaking into a routed profile. Startup checks both process ownership and listener readiness; an uncertain result is not automatically retried. A foreign process occupying port 9119 is left untouched. Stop uses the existing process manager with the exact profile home, not a command-line substring match or global kill.

These controls do not publish a public WebUI, configure a reverse proxy, install browser/system dependencies, or embed the browser-based Dashboard inside a Telegram message. The existing WebUI's profile authorization and session semantics are unchanged.

## Username/password login

`/webauth` configures the bundled `dashboard_auth/basic` provider. Despite its name, it uses a password login form and signed sessions, **not HTTP Basic Authentication**. Passwords are hashed with the provider's existing scrypt implementation, not stored in plaintext. A configured password must be verified before replacement. New credentials require a username, two matching password entries, and a separate **Save credentials** button.

The confirmation explicitly enables `dashboard.require_auth_on_loopback: true` for that profile as well as saving the password hash. The existing WebUI authentication gate reads this opt-in; other profiles keep the default `false`, and existing non-loopback/public-URL requirements and Desktop-owned backend exemptions remain unchanged. Restart the owning Dashboard to apply changed credentials or the gate. Existing signed sessions are not forcibly revoked by a password change.

Credentials are entered through a private bot chat, not a Telegram Secret Chat. Input messages are deleted before validation; capture is cancelled if deletion cannot be performed. They are intercepted before model turns, attachment processing, and ordinary setup editors. The UI never prints passwords or password hashes. Expired or duplicated input is consumed safely rather than treated as an agent request. Profile-owned environment overrides are reported as managed settings instead of silently writing ineffective YAML values.

## Native thread management

The native manager uses Telegram's forum-topic APIs as the topic source of truth and retains Hermes' existing session routing. It does not keep a parallel durable topic registry.

- `/thread create <title>` creates a native topic through the existing adapter service.
- `/thread list [page]` provides paginated native topics with their IDs and status.
- `/thread open <id>` or `/thread select <id>` opens the selected topic's conversation context.
- `/thread rename <id> <title>` changes its title.
- `/thread close <id>` and `/thread reopen <id>` control ordinary forum topics.
- `/thread archive 1` and `/thread unarchive 1` hide/show Telegram's General topic where supported. Other topics use Close/Reopen; the UI does not pretend Telegram provides an archive API for them.
- `/thread delete <id>` opens a separate irreversible-deletion confirmation. Only its fresh, originating-card confirmation can delete that topic's Telegram history, once. Cancelling changes nothing. General cannot be deleted through this UI.

The existing `/topic` DM session-mode command remains separate and is not shadowed by `/thread`. Topic creation/listing/editing depends on Telegram's threaded-mode or forum capabilities and the bot's real permissions; the control centre cannot grant those permissions itself.

## Native UI and callbacks

Cards use MTProto `InputRichMessage` HTML, native styled callback buttons, headings, dividers, and tables. Pagination footers are final italic text in the form `Page n of N • total T`, before the keyboard. Rendered counts derive from the actual data rather than fixed labels.

Callbacks are checked against the owner, profile/home, chat/topic, originating message, and expiring state. Missing callback topic metadata can be restored only after an exact card match; typed inputs never borrow another topic's pending edit. Mutating callbacks are one-shot. Buttons show working and final card content; alert-only acknowledgements are not used as a substitute for operation results. When delivery or a mutation is uncertain, cards tell the owner to inspect/refresh before retrying rather than falsely claiming rollback or sending another irreversible request.

## Validation

### Explicit large-file downloads

`/tgfiles metadata <message ID>` inspects media without downloading it. `/tgfiles download <message ID>`
and the `files_download` client operation use the existing Telethon MTProto route, not cloud Bot API
`getFile` or a local Bot API server. There is no application file-size ceiling on this explicit route.
In DMs and groups, reply directly to the file with `/tgfiles` (metadata), `/tgfiles metadata`,
or `/tgfiles download`; no visible message ID is needed. Explicit IDs must belong to the
originating chat/topic and profile. Neither the command's own ID nor its response-delivery
reply ID is used as a file target. No other chats or Telegram identities are searched.
Telethon writes bounded chunks to a private temporary file, then the operation checks completeness
and atomically publishes a unique cached file (directory `0700`, files `0600`). Multi-file captions,
names, MIME types and sizes remain per-message metadata; this does not enable large auto-downloads
or change the separate attachment-intake personal/operator policies.

The progress card has a cancellation button bound to its actor, chat/topic, profile and message.
It shows elapsed seconds and measured average throughput, with short progress lines for phones.
Completion cards retain file metadata but omit absolute cache paths; the internal operation result
still carries the cached path for authorized downstream use. The owner's 42,012,051-byte APK is
a successful native-MTProto baseline, not a failed download or a measured before/after benchmark.
Cancellation, insufficient storage/quota/filesystem size, incomplete media and 120 seconds without
media progress are errors displayed in code blocks; partial downloads are removed. Slow downloads
can continue indefinitely while making progress. Finished cache files are retained; no automatic
eviction or resume is added.

[Telethon documents filename/stream output and progress callbacks](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.download_media).
Documents of at least 1 MiB now use four concurrent 512-KiB MTProto part requests through
Telethon 1.45's `iter_download` API. A rolling window replaces each consumed part immediately,
instead of waiting for an entire four-part batch. Parts are written in offset order; completed
out-of-order parts count against the same bounded window, never an unbounded reorder queue.
A per-operations semaphore also bounds overlapping downloads.
Transient failures retry up to four attempts with backoff; flood waits honor Telegram's delay.
Announced retry waits extend the idle deadline; scope and cancellation checks continue during them.
Expired references are refreshed only after verifying the same chat/topic, message, document
ID and size. Other media/small documents retain the native writer; CDN redirects cancel the
parallel workers before handing the transfer back to Telethon's native media handler.

This is a first-party implementation of the [FastTelethon parallel-chunk idea](https://gist.github.com/painor/7e74de80ae0c819d3e9abcf9989a8dd6).
The gist declares MIT permission but targets Telethon 1.21 and manually constructs private
senders and authorization requests. None of that source is vendored: the existing client's
sender pool is reused, with no new login or custom authorization path. The one pinned private
compatibility hook is Telethon 1.45's CDN redirect exception, used only to select its native handler.
[Telethon warns](https://docs.telethon.dev/en/stable/quick-references/faq.html#file-download-is-slow-or-sending-files-takes-too-long)
that parallel transfers can encounter flood waits sooner. Telegram/DC/account capacity, network,
disk and encryption CPU can still dominate throughput; four requests do not guarantee a 4x speedup.

There is no 25 MiB or 2 GiB download guard. [Telegram documents 2 GB ordinary uploads and 4 GB Premium
uploads, downloadable by all users](https://telegram.org/blog/700-million-and-premium#4-gb-uploads).
These are upload limits, not a promise that every media type, account permission, backend or network
will deliver a particular file. Backend RPC errors remain visible; no protocol fallback is attempted.

Parallel/download/DM validation against baseline `e3a23e59cbc05e5eb54007cfa7b6f6038278ce0a`:
the final native Telegram plugin, retained Telegram gateway, shared platform and Telegram tool
regression run passed **1,212 tests across 78 files**. Two unrelated baseline-broken files were
excluded from that final run after being reproduced: `test_telegram_connect_success_visibility.py`
expects legacy connection-log text absent from the unchanged adapter, and
`test_telegram_mention_context.py` imports the already-missing `test_telegram_group_gating` module.
New tests use mocked MTProto responses, the real pinned Telethon iterator, temporary profile
directories and deterministic concurrency barriers. They cover ordering/short or invalid parts,
backoff and flood waits, reference refresh, cancellation cleanup, same-DM explicit/reply IDs,
cross-chat rejection and profile isolation. This is not a live network throughput benchmark or
a live multi-gigabyte transfer test. No Telegram message, restart or deployment was performed.

### Speed remains a measured, not assumed, outcome

After `aff8ac3e8ba9dcbe5f4a2eba581fda8a2fa3606a`, the owner reported a completion card with
42,012,051 bytes, `elapsed_seconds=19.651554615003988` and
`average_bytes_per_second=2137848.7261218377`: **2.0388 MiB/s (17.1028 Mbit/s)**.
That is owner-supplied card evidence, not a live transfer performed by this investigation.
No pre-parallel elapsed-time measurement exists here, so no download speedup has been established.
The available gateway journal and log files contain no matching per-transfer timing, flood-wait
or edit-latency records that would let us attribute those 19.65 seconds conclusively.

Confirmed limitations in that implementation were a barrier after every four parts and synchronous
progress-card edits inside the writer callback. The rolling window removes the batch barrier;
progress edits now use one cancellable, coalescing worker with at least three seconds between
edits. Final/cancel/error paths stop and gather that worker before any terminal card edit.
The shared semaphore, ordered writer, profile/chat checks, retry delays and partial-file cleanup
remain. Sequential writes are retained for correctness; their cost is now timed rather than
assumed to be the bottleneck. The pinned iterator's request size remains 512 KiB; increasing
concurrency blindly or bypassing its request-size handling is not a demonstrated fix.

Four concurrent requests **do not mean four independent DC connections**. Telethon's cached
sender is shared per DC (the home DC uses the existing sender). This intentionally avoids
FastTelethon's custom sender/exported-authorization implementation. The same Telegram account/DC,
connection, network route, server limits and flood waits can still bound throughput. No new
sessions, parallel identities, custom authorization flow or connection fan-out were introduced.

The actual gateway interpreter had Telethon 1.45.0 and no `cryptg`; its AES backend was `libssl`,
not pure-Python `pyaes`. The libssl ctypes wrapper still copies buffers through Python objects.
An **offline AES-IGE-only** comparison in the test environment processed 8 MiB with libssl in
1.3215 seconds (6.05 MiB/s), versus cryptg in 0.01515 seconds (528.08 MiB/s), with cross-backend
plaintext verification. This isolates crypto overhead, not MTProto framing/network or download
throughput, and must not be advertised as a Telegram speed multiplier.
[Telethon recommends cryptg](https://docs.telethon.dev/en/stable/quick-references/faq.html#file-download-is-slow-or-sending-files-takes-too-long);
`cryptg==0.6.0` is now pinned in `[messaging]` and `uv.lock`. It is a native crypto extension,
not a second Telegram client. Only the test `.venv` was updated; the running gateway's `venv`,
loaded crypto backend, profile configuration and lifecycle were not changed. Existing installations
need dependency synchronization and a separately authorized restart to select the accelerator.

Future completed operation results and INFO-level local transfer log lines include `download_diagnostics`:
strategy, sender mode, selected crypto backend, part/window sizes, retry count/requested delay,
aggregate part-request and semaphore-wait seconds, `output.write` time, and progress-callback time.
Concurrent part times are sums and may exceed wall time; they are not direct network-RTT samples.
These diagnostics omit credentials, file paths and message content and stay out of the phone's
completion-card payload. A native/CDN fallback is labeled explicitly. They enable a future
authorized live comparison; **none has been run for these follow-up changes**.

Follow-up validation: **1,219 tests passed across 80 Telegram-related files**, excluding the same
two previously proven baseline-broken gateway files above. A final focused recheck passed 34 tests;
the lockfile check passed, with cryptg as the only added package and no version changes to existing
packages. New event/barrier-driven tests prove rolling refill before an earlier batch's slow tail,
bounded out-of-order buffering, blocked progress edits not blocking transfer, coalescing/throttling,
and progress-worker cleanup before terminal edits. Eight packaging metadata tests passed; its
remaining exact-pin exemption assertion still reports the baseline's missing `pymongo` and
`python-socks` whitelist entries (reproduced from the prior commit), not cryptg. Independent
review found no concrete blockers. None of these checks measure live Telegram download speed.

### Independent connection follow-up

The owner's already-running transfer completed on `2f3db5e` with **929,131,622 bytes in
208.366 seconds (4.2526 MiB/s)**. Its existing completion log reports cryptg, no retries,
734.095 aggregate part-request seconds, 0.0209 semaphore-wait seconds, 0.8455 disk-write
seconds and 26.3409 progress-callback seconds. The overlapping request-time sum is about
3.52 times wall time: requests genuinely overlap, but this does not establish wire-level
parallelism or prove that the shared connection is the dominant live bottleneck. Disk writes
and semaphore contention were not large costs in this transfer. Progress callbacks occupied
12.64% of wall time; that counter includes all callback work, not just Telegram message edits.
This is a baseline observed from existing logs, not a new transfer started by the investigation.

The UI callback previously repeated full authorization/profile resolution for every part after
the operations callback had already checked it. Coalesced byte snapshots now avoid that duplicate
lookup; operations still checks scope on every part and progress event, and the UI rechecks before
each external edit (including inside its edit lock) and terminal edit. Cancellation and progress
throttling remain. The measured 26.34 seconds cannot all be attributed to the eliminated lookup.
Operations now records `scope_check_count` and `scope_check_seconds_sum` separately so the
remaining authorization/profile-resolution cost can be measured, not guessed. These counters
include initial scope validation before the transfer clock starts and overlap other timing
categories; they must not be added to request/progress time as disjoint wall-clock costs.

`download_connections.py` now provides at most **four independent download senders across concurrent
operations on one adapter**, while retaining the four-part/2-MiB ordered buffer and request semaphore.
It uses Telethon's own `MTProtoSender`, native `_call` and native download iterator, not another
Telegram library or transport. Authorization keys are obtained only for the intended ordinary DC:
the primary sender's key for its own DC, otherwise Telethon's authorized borrowed sender for that DC.
Each dedicated sender has a separate copied key object, connection and MTProto session state; no key
is copied across DCs, no main session/configuration is written, and no custom login/export/import
flow is implemented. Connection/proxy/local-address and retry options come from the existing client.
An initialization-request copy prevents mutation of the main client's request. Native migration
may temporarily use the original client's borrowed sender; the facade balances every borrow even
after repeated redirects. CDN redirects retain native fallback. Initialization and dedicated media
calls explicitly use `InvokeWithoutUpdatesRequest`, with initialization nesting matching Telethon's
own no-updates mode. Any unrelated unsolicited updates still enter the same client's existing gateway
queue instead of being dropped. Native request retry/flood handling remains active; the wrapper conservatively
shares its flood-wait cache key across dedicated media calls.
Dedicated senders close after the final active transfer, including errors/cancellation, and never
disconnect the primary sender. Finite teardown is awaited through repeated cancellation and restores
every queue slot before cancellation propagates; a blocked-disconnect regression proves a later
transfer can reuse the pool. No download starts implicitly and no file-size ceiling is added.

**Telethon does not offer a public independent-sender download API.** This small adapter deliberately
uses private internals audited for exactly `Telethon==1.45.0`; other clients/versions retain the
shared-sender iterator path. Re-audit it before changing the pin. It is not a promise of server-side
bandwidth or a guaranteed live speedup. Account/DC/network limits and head-of-line waiting needed
for bounded ordered writes can still dominate.

`scripts/benchmark_telegram_download.py` is an entirely offline harness using the production
downloader, genuine Telethon client/iterator/request handling and simulated sender transport.
It compares shared and independent senders at concurrency 1, 2 and 4 with controlled latency,
per-connection bandwidth and a shared-link limit, verifies ordered output, and reports peak
in-flight requests. Its synthetic connection-limited gains must not be advertised as live Telegram
results; the shared-link-limited case demonstrates why additional connections can stop helping.
No new Telegram transfer or gateway restart is authorized or performed for this follow-up.

An offline run with 8 MiB plus a 37-byte final part and 30 ms simulated RTT produced these MiB/s
values. Every case verified the same size/SHA-256; observed request peaks were exactly 1/2/4,
with one shared sender versus 1/2/4 independent senders. Results include the model's tandem
per-connection and shared-link transmission delays, not a model fitted to the owner's network.

| Synthetic constraint | Shared sender: concurrency 1 / 2 / 4 | Independent senders: 1 / 2 / 4 |
| --- | --- | --- |
| 8 MiB/s per connection, 64 MiB/s shared link | 4.74 / 7.58 / 7.60 | 4.75 / 9.23 / 17.93 |
| 64 MiB/s per connection, 4 MiB/s shared link | 2.98 / 3.88 / 3.88 | 2.99 / 3.87 / 3.87 |
| RTT-only, no intentional bandwidth ceiling | 14.74 / 27.69 / 48.03 | 14.84 / 27.46 / 47.52 |

Final validation passed **1,222 tests across 82 Telegram-related files**, with only the two
already-documented baseline-broken gateway files excluded. The hardened focused pass passed
53 tests; the default 18-case benchmark and an additional 18-case 1-MiB/5-ms short-file run
verified every output. Tests cover distinct home/remote-DC senders, reuse/cross-transfer caps,
unchanged primary authorization/session, request update suppression, cancellation during setup
and repeated cancellation during final disconnect, repeated migration borrow accounting, native
CDN fallback, reference refresh, ordered writes, explicit targeting and profile boundaries.
No gateway restart or new live transfer was performed, and no live speedup is claimed.

### Pinned transport policy

The only runtime implementation is Python `Telethon==1.45.0`, pinned in both dependency extras and
`uv.lock`, and checked again before client construction. Bot tokens remain supported through
`TelegramClient.start(bot_token=...)` over MTProto. The dormant HTTP adapter, its HTTP fallback and
polling/webhook machinery have been removed rather than left as a selectable fallback.

`scripts/run_tests.sh` first runs `scripts/check_telegram_transport.py`. The gate rejects HTTP Telegram
client dependencies/imports (including literal dynamic imports), HTTP endpoint/configuration paths,
and inconsistent Telethon dependency/lock/runtime pins. Its own tests exercise forbidden fixtures;
it performs no network requests. The historical HTTP-only wire eval and polling/network-specific
tests are obsolete; native MTProto, profile-isolation, attachment and command suites remain.

The focused suites live alongside the implementation:

- `tests/plugins/platforms/telegram/test_control_center.py`
- `tests/plugins/platforms/telegram/test_setup_ui.py`
- `tests/plugins/platforms/telegram/test_standalone_ui.py`
- `tests/plugins/platforms/telegram/test_thread_ui.py`
- `tests/plugins/platforms/telegram/test_web_auth_ui.py`
- `tests/hermes_cli/test_dashboard_auth_profile_gate.py`

Run them with `scripts/run_tests.sh`, together with the existing Telegram, command-registry, topic-routing, and Dashboard-auth suites. Tests use temporary profiles and mocked transports; they do not send Telegram messages or alter the operator's runtime configuration. Real client pixel rendering still requires a visual check in the owner's Telegram client after the gateway is restarted.

### Authorization hot-path follow-up

The existing gateway completion log at 2026-10-22 19:41:10 records **929,131,622 bytes
in 160.114 seconds**, about **5.80 MB/s (5.53 MiB/s)**, on `444ec60` with four
independent senders and cryptg. No new transfer was started for this investigation.
It records 12,569 synchronous scope checks consuming 105.889 seconds in aggregate,
13.884 progress-callback seconds and 0.745 disk-write seconds, with no retries.
Scope-check time overlaps the request/acquisition/progress timing categories; these
numbers must not be added together as independent elapsed-time costs.

The measured hot path repeatedly parsed the same profile policy and reconstructed
local tenant storage. Policy reads still read `.env` bytes on every call, but a bounded
128-home cache now reuses parsing only when the SHA-256 of those exact bytes matches.
Only owner/allowlist fields are retained, not provider secrets. File deletion/read
errors and same-size, same-mtime edits remain immediately observable. HF runtime
overlays and conflicts are checked anew, and live authorization/ban decisions are
never cached. Store construction uses its already-read owner policy and no longer
repeats the constructor's owner initialization. An established owner is freshly read
without `BEGIN IMMEDIATE`; missing-owner creation still rechecks identity under the
write transaction before inserting. All per-part scope/cancellation checks, the
four-sender/2-MiB buffer bound, transport, retry behavior and large-file handling remain.

One offline workload of 1,000 real local-policy/SQLite checks, using a temporary
synthetic profile with 100 unrelated placeholder `.env` entries, took 3.089 seconds
on `444ec60` versus 1.875 seconds after these fixes: about **39% less authorization
check time in that workload**. This is not a Telegram throughput benchmark, a
network speed prediction, or evidence of a new live transfer speedup. The running
gateway remains on `444ec60`; applying the new Python code requires a separately
authorized restart.

Final validation passed **950 tests across 65 Telegram/policy/tenant files** with no
exclusions. The broader seven-file local-runtime/overlay/isolation pass initially
had 67 passes and six Unix-socket bind failures caused by the test runner's long
temporary directory; all 23 tests in that socket file passed with a shorter pytest
`--basetemp` inside the checkout. No unrelated production code was changed for that
test-environment issue. New regressions proved the old owner read required a write
transaction and the old policy path reparsed unchanged bytes; both pass with these
fixes. The callback admission fixture now supplies real temporary profile policy
instead of mocking an internal owner lookup, preserving its live storage path.

### Post-e7ae17c ramp investigation

The owner's next existing transfer completed at **2026-10-22 20:22:29.340**:
929,131,622 bytes in 166.394 seconds, **5.58 MB/s (5.33 MiB/s)**, with cryptg,
four independent senders, four peak active part requests and no retries. This was
6.280 seconds longer than the earlier 160.114-second run; it is not evidence of a
live speedup, nor does a single pair of transfers establish a causal regression.
The owner's report of staying below 4 MB/s for roughly 40 seconds and reaching
5 MB/s around 50 seconds remains a user-observed reading, not a sampled network
trace. The Telegram progress card calculates a cumulative bytes/elapsed average,
not instantaneous throughput, so a rising card value cannot locate a bandwidth step.

The completion time minus its elapsed duration implies a start around 20:19:42.946.
A borrowed DC sender connected at 20:19:43.285–44.804; four dedicated connections
completed at 20:19:45.398–45.399. The first native iterator-start log was at
20:19:45.665, about 2.7 seconds after the inferred start. Sender setup therefore
does not account for a 40-second warm-up. During the first 60 seconds, 623 unique
512-KiB iterator-start entries covered offsets 0 through 326,107,136, with no
inter-entry gap exceeding one second. These entries precede iterator consumption:
they are neither exact RPC-send timestamps nor received-byte samples. Existing
logs cannot prove the first-minute receive-speed curve or its exact cause.

Diagnostics still recorded **12,573 synchronous scope checks totaling 113.709
seconds**, versus 0.713 seconds in output writes. Scope checks overlap the
request/acquisition/progress measurements; do not add the categories. Inspection
found an avoidable path: `LocalTenantStore.access_for_scope` loaded and parsed
`config.yaml` for every group check, including the owner and independently granted
users. Group `allow_all` only adds access and cannot revoke these grants or override
a ban. The lookup now consults group YAML only for an otherwise-unallowed, unbanned
group/forum actor. Fresh tenant/ban reads, profile routing and policy byte checks
remain on every call; no authorization result is cached. No part, cancellation,
reference-refresh, integrity, connection-limit or transport safeguard is removed.

A synthetic **10,996-byte** config and local SQLite fixture exercised the real
operations authorization -> Telethon adapter -> local tenant path, without any
network transfer. For 100 checks per grant/mode, mean check time changed from
5.602 to 2.056 ms for the owner, 5.866 to 2.690 ms for a global grant, and 6.335 to
2.587 ms for a group-scoped grant. A separate 50-owner-check cProfile pass observed
50 settings/config-loader calls before and none after. These are local workload
results, **not promised Telegram transfer speeds** or an attribution of all 113.709
seconds to YAML parsing.

New real-adapter regressions reproduce redundant YAML reads on the old code for
owner/global/scoped grants, then verify no such reads after the fix. They cover
A -> B -> A homes, independent-writer ban/unban visibility, grant revocation,
exact-group guest fallback and live `allow_all` changes. Validation with
`scripts/run_tests.sh -j 4 --file-retries 0 tests/plugins/platforms/telegram/
tests/agent/test_local_tenants.py tests/agent/test_local_tenant_owner_reads.py
tests/gateway/test_mongo_multiuser.py tests/gateway/test_multiuser_policy_hot_path.py`
passed **930 tests across 64 files**, with no failures or retries.
The live gateway remains the same process started at 20:17 on `e7ae17c`;
no restart, new Telegram transfer, credential/provider change or HF deployment
was performed. This source fix requires separate restart authorization to take effect.

### Six-way download-window trial (after e14fe2d)

The owner's real post-restart `e14fe2d` transfer was **929,131,622 bytes in
166.682 seconds (5.32 MiB/s)**, essentially unchanged from the earlier
166.394-second run. The authorization fixes therefore did not demonstrate a
Telegram throughput gain. No new Telegram download was started for this trial.

The next conservative source configuration is **six 512-KiB parts per ordered
window**, with **six concurrent requests and at most six independent senders
shared across concurrent operations on one adapter**. The pool hard ceiling and
window derive from one `MAX_DOWNLOAD_CONNECTIONS = 6` constant; the existing
operations constructor derives its semaphore and pool capacity from that window.
Telethon's 512-KiB per-request limit, four-attempt retry budget and crypto pins
are unchanged. The retained part payload bound grows from 2 to **3 MiB per
transfer**, not 3 MiB for all transfers combined; transport/Python buffers add
other memory overhead. Completed out-of-order parts still occupy the window.

An **offline model**, using the actual `_download_document` scheduler with a fake
iterator transport, compared four, six and hypothetical eight-part windows. Each
trial wrote and SHA-256-verified a 24-MiB synthetic local file (48 parts); numbers
below are medians of three trials. Every request had a 30-ms synthetic latency;
a shared lock serialized a 1-ms payload service interval in the latency cases,
or a 12-ms interval in the bandwidth-limited case. The variable-latency case
tripled latency for part indexes congruent to 1 modulo 9. The eight-part variant
changed only the local model's scheduler/semaphore, bypassing sender creation;
**eight remains disallowed by the production pool**. No real sockets, Telegram
account, authorization workload, sender setup or server throttling were modeled.

| Synthetic case | Four parts | Six parts | Eight parts |
|---|---:|---:|---:|
| Latency-limited | 0.4009 s | 0.2702 s | 0.2089 s |
| Shared-bandwidth-limited | 0.6617 s | 0.6637 s | 0.6601 s |
| Variable request latency | 0.7286 s | 0.5643 s | 0.5609 s |

Six reduced modeled elapsed time by 32.6% in the latency-limited case and 22.6%
with variable latency, but did not help a bandwidth-limited transport. Eight
helped further under uniform latency but barely helped the variable-latency case,
while doubling the original sender/request burst budget and retaining 4 MiB per
window. Six is selected as a bounded next experiment, **not a proven speedup or
prediction of Telegram performance**. Telegram/account/network bandwidth or
synchronous scope-check work can still dominate. More senders can increase
connection setup cost and FloodWait risk; the existing logs cannot decide which
limit dominates the owner's transfer.

Retry waits still honor the server-provided delay, release both scarce resources
before sleeping, and remain cancellable. Backoff is per part, not an account-wide
cooldown; this trial does not bypass Telegram limits. The semaphore queues waiting
requests and prevents a file from continually taking released slots ahead of
already-waiting work, but this is not a strict per-file round-robin scheduler or
a bandwidth guarantee. Sender identity/DC/auth-key validation, migration borrowing,
update suppression, last-transfer teardown, fresh scope/ban/profile checks,
reference refresh, CDN fallback, cancellation and ordered/integrity handling stay
intact. Two additional senders may be opened for a sufficiently large transfer;
senders are still reused while transfers overlap and closed after the last one.

Tests exercise both four- and six-sender capacities across home/remote DCs,
shared multi-file bounds and waiting-file progress, six-part backpressure,
success/failure/migration/CDN/setup-cancellation cleanup, reusable capacity,
server-delay gating with a second download, cancellation during backoff, and
client/auth-key turnover across the full pool. The six-capacity integration test
failed on the old four-sender ceiling before the production change. Existing
operations tests continue to exercise profile routing, scoped refresh rejection,
private output permissions and partial-file cleanup.

The gateway must remain on the owner's **20:33 restart of e14fe2d** during this
source-only trial. Applying the subsequent commit requires explicit owner restart
authorization. Only a later owner-authorized restart and real transfer can establish
whether six improves upon the 166.682-second/5.32-MiB/s baseline; compare actual
elapsed time, sender/window diagnostics and retry counts, not the early running-
average card alone. No HF deployment, provider or credential changes are included.

Final validation: `scripts/run_tests.sh -j 4 --file-retries 0
 tests/plugins/platforms/telegram/ tests/agent/test_local_tenants.py
 tests/agent/test_local_tenant_owner_reads.py tests/gateway/test_mongo_multiuser.py
 tests/gateway/test_multiuser_policy_hot_path.py` passed **935 tests across 64
files**, with zero failures or retries. The initial broader run had four failures
in the new fairness assertion because its fixture retained mutable native request
objects: Telethon advances their offsets after reading. The fixture now snapshots
each request at send time; the fairness assertion itself remains in place and
passes for both capacities and both DC routes.

A separate cross-check used the existing `scripts/benchmark_telegram_download.py`
`run_case` path (real Telethon client/iterator, fully simulated senders), 4-MiB
files, 30-ms RTT, three trials per case. Four/six-sender median seconds were
0.238202/0.218082 with an 8-MiB/s per-connection and 64-MiB/s shared-link model,
1.051888/1.052794 with a 4-MiB/s shared-link bottleneck, and 0.069502/0.069807
with latency alone. Both widths need two request waves for this short eight-part
file, so even the latency case need not improve. All SHA-256, output-size,
concurrency, sender-count and cleanup checks passed. These additional **offline**
results reinforce that the candidate does not guarantee faster real downloads.

### Restore measured four-way download budget (supersedes six-way trial)

The owner's real six-way transfer on `955f5fe` completed the same
**929,131,622-byte** file in **174.622 seconds (5.07 MiB/s)**. The preceding
four-way `e14fe2d` run took **166.682 seconds (5.32 MiB/s)**: six was **4.8%
slower by elapsed time**. Other four-way observations were `e7ae17c` at
166.394 seconds / 5.33 MiB/s and `444ec60` at 160.114 seconds / 5.53 MiB/s.
These individual observations support restoring four, not promising that any
particular earlier elapsed time will recur. The offline models above did not
predict the real outcome and are not grounds to retain six.

The source now uses **four shared independent senders, four shared active part
requests per adapter, four ordered parts per file window, and 512-KiB parts**.
The retained part-payload bound is back to **2 MiB per transfer**, plus
transport/Python overhead; it is not a global memory bound. The hard pool
ceiling also returns to four, rejecting five and six. Telethon remains pinned
to 1.45.0, whose native iterator clamps request sizes to 512 KiB; increasing a
constant above that limit is not an effective larger-request optimization.

Six-way diagnostics showed six peak active requests, 685.26 seconds summed
part-request time, 32.37 seconds summed connection-acquire time, 32.76 seconds
summed scope-check time, 0.60 seconds disk-write time, 4.40 seconds progress
callback time, and zero retries. Request and connection timings overlap:
these totals cannot be added together or subtracted from wall time to promise
a gain. The diagnostics do not establish whether Telegram/account/network
limits or other contention dominate. Disk writes are not a credible primary
bottleneck in this observation. Scope checks remain fresh: no ban/profile/
guest checks are removed or cached less safely to chase speed.

Only the shared concurrency budget is changed. Authorization optimizations,
policy enforcement, cancellation, retry delays, integrity checks, ordered
backpressure, waiting-file fairness, migration/CDN fallback, and sender cleanup
are retained. New regression coverage explicitly locks the four-way budget,
512-KiB part size, 2-MiB window, and rejection of higher capacities. The real
Telethon iterator's shared-pool tests cover home/remote DCs at two and four
senders; the six-way trial's additional safety tests remain in place.

**No real speedup is proven for this restoration.** No real file download,
new Telegram consumer, gateway restart, HF deployment, provider or credential
change was performed. The live gateway remains on the owner's 21:02 IST
restart of `955f5fe` (six-way). Applying this source requires a new explicit
owner approval to restart; historical restart approvals do not authorize it.

Validation: the new four-way-budget regression first failed on `955f5fe` at
`MAX_DOWNLOAD_CONNECTIONS == 4` (actual value six), then passed with the restored
budget. `scripts/run_tests.sh -j 4 --file-retries 0
tests/plugins/platforms/telegram/ tests/agent/test_local_tenants.py
tests/agent/test_local_tenant_owner_reads.py tests/gateway/test_mongo_multiuser.py
tests/gateway/test_multiuser_policy_hot_path.py` passed **936 tests across 64
files**, zero failures or retries. The runner's transport policy check also
passed: Telethon 1.45.0, Python MTProto only. These are offline regressions,
not a new throughput measurement.

### Completion-driven download scheduling

The owner's latest existing four-way run on `3ab83e0` completed 929,131,622 bytes
in **173.644 seconds (5.10 MiB/s)**, with zero retries. This investigation did not
start another live download. The 571.885-second aggregate part-request time is
an overlapping sum, not elapsed time or proof of a server/network bottleneck.
Both installed environments use Telethon 1.45.0 with cryptg; native crypto and
sender inspection did not establish another safe tuning change. Chunk size and
the shared four-request/four-sender limit remain unchanged.

The ordered writer could stop refilling connections behind one slow early part:
three later results could be ready in memory while only the head request remained
active. A completion-driven writer now writes each validated response at its own
aligned offset, releases its payload, and refills that slot immediately. At most
four part tasks/results remain resident per file (2 MiB payload budget), with the
existing shared semaphore and connection pool still governing concurrent files.
No additional prefetch window, worker, transport, or persistent sender is added.

Progress counts successfully written part bytes, never file position or sparse
file length, so an early high-offset write cannot report holes as downloaded.
Every part still has its expected length checked, offsets are scheduled exactly
once, and the caller only publishes the private file after all tasks succeed and
the final length matches. Existing scope checks, cancellation, flood/retry waits,
reference refresh, native CDN fallback and cleanup remain. The temporary file can
be sparse while lower offsets are pending; it is not a usable partial stream and
is removed on failure/cancellation before publication.

Two event-driven regressions (delayed first and second parts) both failed on the
old production writer: remaining chunks could not finish until the blocked part
was released. Both pass with completion-driven writes, verify byte-for-byte
output including a 37-byte tail, monotonic actual-byte progress, the active-request
bound, and iterator closure. These are offline scheduling proofs, **not measured
Telegram throughput improvements**. All 48 focused downloader/operations/pool/UI
tests passed. A real same-file comparison is still required to establish the gain
on the owner's account/DC/network.

At the owner's request, the `download_limits` result field, corresponding
capabilities prose, and repeated limits footer on download cards are removed.
Storage, idle-timeout, authorization and integrity enforcement are not removed;
no artificial speed or file-size limiter was discovered or disabled.

No live large download, duplicate Telegram consumer, gateway restart or deployment
was performed. The live gateway remains on `3ab83e0` from the owner's 21:12 IST
restart. Applying this source requires a new explicit approval to restart.

Final validation passed **996 tests across 66 files**, zero failures and no retries,
including a direct payload-lifetime test: a blocked progress callback retains no
more than four completed 512-KiB payloads, stops further refill at the bound, and
releases all payloads on completion. The transport policy check and
`git diff --check` passed. Independent code review found no confirmed implementation
bug. Validation command:

```bash
scripts/run_tests.sh -j 4 --file-retries 0 \
  tests/plugins/platforms/telegram/ \
  tests/agent/test_local_tenants.py tests/agent/test_local_tenant_owner_reads.py \
  tests/gateway/test_mongo_multiuser.py tests/gateway/test_multiuser_policy_hot_path.py \
  tests/tools/test_telegram_client_tool.py tests/gateway/test_telegram_client_operations.py
```
