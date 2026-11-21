# Universal profiles — design

**Branch:** `feat/universal-profiles` (from main `799f565e5c`). **Status:** design + build in progress. Do NOT push.

**User order (verbatim):** "Build universal profiling system. Profile means an alloweds or an owner, who have started and have access or authority to handle Hermes."

**In one sentence:** every allowed user (and the owner) who has started the bot gets their own
Hermes home directory (`~/.hermes/profiles/tg-<telegram user id>/`); everything that user owns,
runs, makes, edits, or adds lives in that home. The shared bot, its allowlist, and group routing
do not change.

Research reports (read-only, verified against this tree):
`~/workspace/research_notes/universal-profiles/{routing,groups-cli,admission-state}-REPORT.md`.

## 0. What exists today (verified in code)

- A profile = a `HERMES_HOME` directory (`config.yaml`, `.env`, `memories/`, `skills/`,
  `plugins/`, `cron/`, `logs/`, `sessions/`, browser data). One bot installation = one launch
  profile + optional named multiplex profiles. There is **no per-user home**: every Telegram
  user runs under the same profile home.
- Per-user separation today is only narrow slices: tenant rows in the launch home's
  `tenant-state.db` (`agent/local_tenants.py`), a per-user memory personal overlay
  (`<launch>/personal/telegram/users/<uid>/`, via a `ContextVar` in
  `gateway/telegram_personal_settings.py`), profile-scoped secrets (`agent/secret_scope.py`),
  browser `user_data_dir` anchored at the (shared) profile home.
- Identity is resolved once per inbound event and pinned: `gateway/session_identity.py`
  `resolve_identity` → frozen `RoutingIdentity` (`transport_profile`, `runtime_profile`,
  `authorization_home`, `runtime_home`, `multiplexed`). `_resolve_profile_home_for_source`
  (`gateway/run.py:4237`) returns `identity.runtime_home` first. **Never derive a second
  answer next to the identity; extend the object** (`gateway/AGENTS.md` § Profile scope).
- `profile_routes` (`gateway/profile_routing.py`) already re-homes by `user_id` (specificity 16,
  highest) — but the matcher has **no chat_type**: a `user_id` route matches that user's GROUP
  messages too. So per-user DM routing must NOT be implemented as synthetic config routes.
- Authorization reads the identity's **transport** home (`_authorization_home_for_source`,
  `gateway/authz_mixin.py:417`); mid-turn auth runs under `_under_authorization_profile`
  (`gateway/run.py:3832`). The tenant store that admits a Telegram user is the launch home's
  store, selected by routing, not by the user.
- `/start` is handled adapter-side (`plugins/platforms/telegram/telethon_adapter.py:2028-2108`):
  `store.start_user(id)` (increments `start_count`), `record_started_identity` (the "started"
  proof), `access_for_scope`. Not allowed → 🥴 reaction + pending-start notice to the owner DM
  (`hm:admission:allow:<id>` / `hm:admission:ban:<id>`). The grant is a second write:
  `store.allow_user(owner, id, True)` from the owner's Confirm (`telegram_controls.py:848-900`)
  or `/allow <id>` (`plugins/platforms/telegram/multiuser.py:8-41`).
- Admitted non-owner turns (DM **and** group) go through `gateway/tenant_runtime.py`
  (`handle_tenant_message`, root = launch home, empty secret scope, owner's provider via
  `_resolve_owner_provider`, history in the tenant store). The owner falls through to the
  legacy path (`telethon_adapter.py:2222+`) and the normal gateway turn under the launch home.
- A new named profile is served without restart: `create_profile` publishes atomically
  (staging + rename) and fires `rescan-profiles`; the 30 s watcher reconciles
  (`gateway/run_profile_reconcile.py`); `_multiplex_for_added_profile` (`:355`) flips a
  single-profile gateway into multiplex mode in place when a second profile appears.
- `create_profile(name, no_skills=True)` writes a comments-only `.env` (`_PLACEHOLDER_ENV`,
  no keys), a default `SOUL.md`, and a no-skills marker. A comments-only `.env` counts for
  `named_profile_has_identity` (gateway serving) but NOT for `named_profile_has_servable_identity`
  (serve/TUI host flip). A profile with no bot credential gets **no adapter**
  (`_start_one_profile_adapters` skips it) — a routed satellite by construction.
- Multiplex defaults on (`resolve_multiplex_mode`); `gateway.multiplex_profiles: false` is retired.
  Only the default home runs the multiplexer (`_launch_home_may_multiplex`).
- Session keys: `agent:<ns>:telegram:dm:<chat>`; named profile → `agent:<name>`. Old keys are
  orphaned, not migrated, when the namespace changes; `_recovered_row_allowed_for_active_profile`
  refuses cross-namespace adoption. The agent cache is keyed by session key, not home — a home
  switch without a key change (or eviction) serves the wrong profile's agent.

## 1. Decisions

### D1. Profile identity: `tg-<telegram user id>`
Keyed by the numeric Telegram user id, never the username (usernames change; ids don't).
`PROFILE_ID_RE = ^[a-z0-9][a-z0-9_-]{0,63}$` accepts it; never name a profile `main`.
The name is derived, not stored: `tg-<id>` **is** the mapping — no mapping table, no config
churn, no Mongo dependency. A user's display name/username stays in the tenant row
(`record_started_identity`) and is display-only.

### D2. Routing: derived in `resolve_identity`, DM-only, existence-gated, fail-closed
After the `profile_routes` match, for a Telegram DM from a non-owner, non-bot sender:
compute `tg-<user_id>`; if `profile_exists(name)` (live, non-tombstoned dir with an identity
marker) → `runtime_profile` / `runtime_home` = that profile, `source.profile` stamped;
if not → runtime stays the transport (launch) home. Groups, forums, channels, threads, and
non-Telegram platforms never take this branch — a group message from a profiled user still
runs under the transport home. There is no separate mapping to go stale: the directory's
existence IS the mapping, so there is no "mapped but missing" drop case — a deleted
(tombstoned) profile simply stops routing, and the user degrades to today's tenant-runtime
behaviour under the launch home. An **unmapped** user (no profile dir) falling back to the
launch home is today's behaviour and is safe: the adapter's admission gate drops them
silently as today.

### D3. Creation trigger: the owner's grant, plus a lazy backfill
- **Primary:** the moment `store.allow_user(owner, user_id, True)` succeeds for a user with
  `start_count >= 1` (owner's Confirm on the pending-start card, or `/allow <id>`). Idempotent:
  re-allow finds the existing home.
- **Backfill:** an admitted + started user whose DM arrives with no profile gets one created
  lazily (covers users allowed before this build shipped, and env-allowlisted users).
- **Never:** on `/start` alone (user is `allowed: False`, unvetted, may be banned seconds
  later), on `provision_user` / `start_user` alone, for group-scope grants (`grant_sudo` with
  a group scope is not a DM admission), for the owner (owns the launch home), for bots.
- Key the trigger on the `allow_user(True)` call site, not on a later `access_for_scope`
  read (that dict conflates global-allowed with sudo).
- Creation failure must not break the DM: log a warning and run the turn under the launch
  home (today's behaviour); the next message retries creation.

**Implementation (slice 4, commit on branch `feat/universal-profiles`):**
- `gateway/user_profiles.py::maybe_create_user_profile(store, user_id)` — the single
  trigger helper, never raises. Gates: numeric id; not the store owner; `store.get_access`
  allowed and not banned; `resolve_started_user` present and non-None where the store
  exposes it (Local backend). **Mongo caveat (verified 2026-11-07):** `MongoTenantStore`
  has no `access_for_scope` and no `resolve_started_user` (`get_access` returns only
  `{allowed, banned}`); on a Mongo-primary host the started gate is skipped and the
  allow alone gates creation. The host is Local, so the gate is enforced where it runs.
  The confirm grant itself already assumes `resolve_started_user` (Local-only) today —
  that pre-existing Mongo gap is not this build's to fix.
- Grant site 1 — `hm:allow:confirm` handler (`telegram_controls.py`): after the grant
  try/except commits, before the `_notify_access_granted` notice; guarded on
  `store is not None` (legacy `upsert_allowed_user` path has no store).
- Grant site 2 — `/allow` command (`multiuser.py::handle_command`): after `allow_user`
  when command is allow/revoke and `state` is True. Ban/revoke never trigger.
- Backfill — `TelethonAdapter._maybe_backfill_user_profile(store, source, access)`,
  called in the multiuser branch immediately before the tenant dispatch, inside
  `if actor_id != owner:`, gated on DM + `access["allowed"]` + `access["started"]`.
  The identity for THIS message was pinned before the backfill, so this turn still
  runs under the launch home; the NEXT message routes to the new home (no mid-turn
  home switch — D17). The attachment follow-up dispatch site is a slice-5 question,
  not slice 4.

### D4. Owner migration: none — the launch home IS the owner's profile
Keep the launch home's `.env` exactly as it is (`OWNER_TELEGRAM_ID` = owner, unchanged). Do
not duplicate or orphan the owner's data. The host gateway must be launched from the default
profile, never `-p <user-profile>` (a named launch home refuses to multiplex).

### D5. Authorization stays on the launch home; runtime moves to the user home
Runtime-only isolation, not full tenant isolation. The launch home's `tenant-state.db` stays
the admission authority (`allow_user`, bans, pending-start cards, `authorization_generation`
re-checks). The per-user home must never get its own tenant store consulted for admission:
`TelethonAdapter._multiuser_home` resolves via the **authorization** home, not the runtime
home (today it resolves via `_resolve_profile_home_for_source` = runtime home — that is the
one adapter change this decision forces). Every `_chat_settings` / `_is_explicitly_allowed` /
`/start` / access-command store read follows `_multiuser_home` and therefore the launch store.
**Audit (done in slice 3):** the other `store_for_home` call sites either take the launch
home explicitly (`tenant_runtime`, `telegram_controls`, `telethon_adapter:831`,
`mongo_accounting`) or take a home whose "owner" is that home's own owner — coherent under
D6 (`mongo_environment` via `build_profile_secret_scope`, `mongo_runtime.active_owner`,
`telegram_personal_settings` and `telegram_user_settings_store`, all Mongo-primary or
per-user-settings paths; inert on the local-backend host). No caller passes a Telegram
turn's runtime home to a store read in a way that crosses users.

### D6. Per-user `.env`: `OWNER_TELEGRAM_ID=<that user>`, nothing else
The user is the owner of their own profile. Never copy the launch `.env` (it carries the
owner's id *and* the owner's tokens). The home starts with an otherwise empty `.env`
(comments placeholder + the owner line, mode 0600). Consequences, all intended:
`enabled(user_home)` is True, `owner_user_id(user_home)` works, the profile counts as
servable (`named_profile_has_servable_identity`), and the Mongo-primary personal-settings
path works. Admission can never be granted from the user home's store because no admission
read ever targets it (D5) — the build audits every `store_for_home` / `enabled` /
`owner_user_id` call site that can receive a runtime home and tests it.
**Provider model (decided):** the user home starts with NO provider keys. A profiled user's
turn resolves its provider from the **owner's scope**, exactly as
`tenant_runtime._resolve_owner_provider` does today. The user borrows the owner's provider;
they cannot see the keys (fail-closed secret scope). User-supplied keys need a provisioning
UI that does not exist — explicitly out of scope, flagged for the user.
**Seed-strip (found while building slice 6):** `create_profile` seeds the launch profile's
`model` block — provider, endpoint, AND `api_key` when it lives in config.yaml — into every
fresh profile (`hermes_cli/profiles.py::_seed_model_config` → `profile_model_seed.launch_model_seed`),
and the creation-time migration backs that seed up into the new home's `backups/`. That is an
owner-key leak into the user home (it also made the owner provider resolve from the user home
itself, masking the owner-scope wrap in tests). `ensure_user_profile` therefore strips the
seeded `config.yaml` AND `backups/` from a home `create_profile` just minted, fail-closed
(strip failure → the half-created home is removed, creation returns None). The strip never
runs on an existing home, so a config.yaml the user has since written is untouched.

### D7. Turn model: a profiled DM runs the NORMAL gateway turn under the user home
The tenant runtime is the old isolation for users WITHOUT profiles; profiles replace it for
DM turns. At the tenant-dispatch site (`telethon_adapter.py:2160-2192`), a DM from a profiled
user (identity `runtime_profile` is a per-user profile) falls through to the legacy path
(`:2222+`) like the owner does: UI handlers then the normal gateway turn, under the user
home via `_profile_scope_for_source`. The adapter-side handling that must stay ahead of the
fall-through: `/guestbot` (shared profile toggles, must never reach a per-user home),
`/help`, `/skills`, `/sethome` (silent deny), access/policy commands, the 🥴 denial.
Unprofiled admitted DMs keep the tenant runtime (transitional). Group/forum/channel turns
are UNCHANGED for everyone (tenant runtime for admitted non-owners, normal turn for the
owner, transport home throughout).
**Command-surface audit (part of the build):** the legacy path exposes owner-flavoured
commands a profiled user never reached before. Profile-scoped commands (settings, model,
provider choice, skills, tools) run against the user's own home — intended. Host-scoped
commands (gateway update/restart, plugin installs touching the shared checkout, anything
that writes outside the user's home) must stay gated on the **launch** owner
(authorization home), not the profile owner. `_is_owner_user` reads the runtime home and
returns True for a user in their own profile — that is correct for profile scope and wrong
for host scope; the audit distinguishes the two per command.

**Implementation (slice 5, branch `feat/universal-profiles`):**
- Fall-through at the tenant-dispatch site (`telethon_adapter.py`, multiuser branch):
  `_is_profiled_dm(source)` (DM + pinned `source.profile` is a `tg-<id>` name) → the
  profiled DM skips the tenant dispatch and falls to the legacy path (normal turn under
  the user home); `_is_host_scoped_command(name, arguments)` → silent return (tenant-style
  silent denial). The attachment follow-up site (`_dispatch_authorized_event`) branches
  the same way. The identity for a message is pinned before the slice-4 backfill, so a
  just-created profile does NOT fall through on that message (D17).
- `HOST_SCOPED_COMMANDS` (`gateway/user_profiles.py`): the explicit host set — the old
  `TENANT_OWNER_ONLY_COMMANDS` minus the profile-scoped five (`skills, memory, memories,
  msettings, stop`, which act on the user's own home and run) plus `logs, hf, webauth` and
  the nine Telegram operation commands (`poll, tgmessages, tgfiles, tginfo, tgadmin,
  tgprofile, managedbots, botpeers, b2b`); `goal gate add` is host-scoped by subcommand, as
  the old policy had it. The set is explicit because the no-limits rework (main
  `bf0b8ef54b`) deleted `TENANT_OWNER_ONLY_COMMANDS` and `tenant_command_policy` no longer
  returns `"owner"` — there is no policy left to derive the host scope from. **Rebase
  decision:** the no-limits branch also removed the launch-owner gate from
  `operations_routing` (both gates); slice 5 keeps the gate. That is not an agent limit —
  client/bot operations drive the host owner's Telegram clients and bots, and one user
  must not operate another user's accounts. Same principle as D5 and the allowgc sudo
  rule ("Sudo needs a real profile").
- New seam `TelethonAdapter._is_launch_owner_user(user_id, source)` — the owner of the
  AUTHORIZATION home (`_multiuser_home`); fail-closed with no auth home. Converted gates:
  `diagnostics_ui._is_owner` (`/logs`, `/hf` — host logs); `control_center._handle_control_command`
  split (browser/dashboard/webui/webauth → launch owner; setup/setuptools/thread → runtime
  owner); `standalone_ui` host actions (`browser-connect`, `browser-disconnect`,
  `dashboard-confirm-start/stop` — CDP attach + WebUI process; profile selections stay
  runtime-owner); `web_auth_ui._allowed` (pairing grants WebUI access); `operations_routing`
  both gates (client/bot operations, incl. the agent-tool entry point).
- Reviewed, left on the runtime-owner gate (profile-scoped, verified): setup_ui /
  setup_tools_ui (config writes to the runtime home; setuptools never installs);
  model/provider/mcp/settings/skills UIs (runtime home); plugin installs
  (`_plugins_dir()` = `get_hermes_home()/plugins` — the runtime home under profile scope);
  telegram_controls allow/confirm handlers (cards live in the owner's DM; tokens bind
  actor+profile+scope); `control_center._dispatch_control_command` / `_handle_control_callback`
  (the host surfaces behind them — logs, standalone host actions — are independently gated).
- **OPEN CAVEAT (needs a ruling, not fixed here):** a profiled DM's normal turn is the
  FULL agent, including the `terminal` tool — a shell on the host, not chrooted to the user
  home. Scope binding (`_profile_runtime_scope`) keeps secrets/config/memory inside the
  user home, but raw filesystem reads via absolute paths are the pre-existing multiplex
  model (a named profile's turn has the same reach; its operator IS the owner). For a
  non-owner profiled user this is NEW exposure versus the tenant runtime's bounded tools.
  Bounding the profiled turn's toolset (tenant-style tool policy under the user home) is
  a follow-up decision; slice 5 does not re-sandbox the agent.

### D8. Memory: the personal overlay moves into the user home, semantics unchanged
At creation, migrate `<launch>/personal/telegram/users/<uid>/{settings.json, memory/, skills/}`
into `<user-home>/personal/telegram/users/<uid>/` (copy, never symlink — the symlink checks
reject them), then delete the launch-home copies (they are the user's data, not the owner's;
keeping a live second source would split memory). `set_personal_memory_context` keeps being
called with the runtime home, so a profiled DM's memory dir is
`<user-home>/personal/telegram/users/<uid>/memory` — the DM personal scope is now the
profile's own memory, as specified. Its fail-closed behaviour stays: an unresolvable
personal dir refuses the DM turn, never falls back to profile memories. The profile-level
`<user-home>/memories/` (MEMORY.md/USER.md) is the profile scope (CLI/group turns in that
profile). A fresh memory-provider instance is bound per user home; the launch home's
instance is never rebound.

### D9. Sessions: new namespace per user; history migrates, never silently orphaned
A profiled DM's session key becomes `agent:tg-<id>:telegram:dm:<chat>` in the user's own
`state.db` (per-profile DBs already exist — `_open_session_db_for_profile` reads
`profiles/<name>/state.db`; slice 2 pins the key, slice 8 pins the DB path). Past DM
history lives in the tenant store (`history:` / `turn:` records under the launch home,
room key derived from the launch home path). **Implemented as a lazy seed, not at
creation (deviation, decided while building slice 8):** on a profiled DM's first turn,
when the loaded transcript is empty, the turn replays the tenant history rows (last 40,
normalized to plain `{role, content}`) from the live launch store. Lazy is strictly
more complete than at-creation — it includes the backfill turn's rows, which still ran
under the tenant runtime — and it is idempotent: a once-per-user-home marker file
(`.tenant-history-seeded`) means a later session reset never resurrects history the user
deliberately cleared, and a failed first turn re-seeds on retry. The launch store is
never written: the old history stays preserved read-only there, and a tombstone fallback
routes back to it. **Eviction (decided while building slice 8):** there is no cached
agent to evict for a Telegram DM — tenant turns build a fresh agent per turn
(`TenantRuntime._make_agent`), and the gateway's in-process cache never holds the old
launch-home key for a non-owner DM (non-owner DMs never reach the legacy path except
through the profiled fall-through). The `_evict_cached_agent_at_boundary` machinery
(commit `on_session_end` under the owning profile's scope) stays available for any
future path that does cache across the switch; nothing in this build needs to call it.

### D10. Browser: free under the turn scope; creation strips the two sharable keys
`user_data_dir()` reads the effective `browser:` config under the turn's runtime scope, and
under the `browser.profile_persistence: save` default it resolves to `<home>/browser` — a
profiled turn gets its own persistent browser data automatically. At creation the generated `config.yaml` (fresh
defaults) must NOT carry an absolute `browser.user_data_dir` from the launch config (that
would merge every user's cookies/logins into one Chromium dir) and must force
`browser.use_real_profile: false` (the real profile is the host owner's OS Chromium
profile — inheriting it would hand every admitted user the owner's logins). No new launcher
parameter is needed. **Verified while building slice 9:** the model seed never copies the
`browser:` section in the first place (`launch_model_seed` copies the `model` block plus
the selected provider entry only), and the slice-6 strip removes the seeded `config.yaml`
entirely — so a fresh user home carries no browser config at all, and `user_data_dir()`
resolves to `<user home>/browser` (the user's own persistent dir, never the owner's) with
`use_real_profile()` False under the user scope. A user
who later configures their own dir gets it anchored at their own home.

### D11. Skills: empty `skills/` + default auto-load + migrated personal skills
A fresh profile starts with an empty `skills/` (bundled catalog still indexed;
`telegram-rich-messages` auto-loads as the default). Personal skills migrate with the
personal dir (D8). Profile skills stay first in precedence so a personal skill can never
shadow a shared one. Auto-load resolves per turn under the new home's scope, never cached
across homes. `_SHADOW_CHECKED` (`agent/skill_utils.py:480-487`) is process-global and not
home-keyed — audit before user count grows (flagged, not fixed here).

### D12. Cron: the user home joins the existing ticker
Per-user homes live under `profiles/` so `profiles_to_serve()` enumerates them; the host
ticker ticks each under `_profile_cron_scope` + `_install_fire_secret_scope` like any named
profile. A user-added job runs with the user home's `.env`/`config.yaml`/skills and delivers
through the user's own session (new namespace, D9). Cron sessions keep `skip_memory=True`.
No per-user home gets gateway identity files (`gateway.pid`, `gateway_state.json`) — a
stray one would make `_cron_profile_gate` stand the host ticker down for that home.

### D13. Groups: unchanged
No sender-based routing for group/forum/channel/thread. Group turns stay on the transport
home (launch home today; the allowgc shared group profile on its own branch). The group
`<user>` session-key tail is conversation isolation inside one home, never a home selector.
Regression-test a group message from a profiled user in the same run as their DM.

### D14. CLI / non-Telegram: launch/default profile unchanged
`hermes chat`, `hermes send`, TUI, `serve`, cron fires: no sender-based routing. A per-user
profile is addressable by name (`hermes -p tg-<id> …`) like any named profile, never
inferred from a Telegram sender.

### D15. Serving: create → rescan, no restart
`create_profile` already fires `rescan-profiles`; the build fires it explicitly after the
migration writes land. A first user profile flips a single-profile host into multiplex mode
in place (`_multiplex_for_added_profile`). User profiles are served named profiles:
channel-stripped, no bot token, no adapter, never connect. If a user profile ever gains a
Telegram token, the claim sweep / credential-absence skip must refuse it — never silently
serve a second client on the shared token.

### D16. What stays in the launch home (never per-user)
Host gateway identity (`gateway.pid`, `gateway.lock`, `gateway_state.json`,
`served_profiles`), token-scoped locks, the multiplex enumeration, the launch-home pin
(`pin_process_hermes_home` — never re-pin per user), update receipts, `LOCAL_RUNTIME_ROOT_DIRS`
(`models/`, `runtimes/`, `node/` — never copy per user), the ticker thread, the tenant store
itself (D5).

### D17. A home switch is a session boundary
Prompt-caching invariant: the system prompt is byte-stable for a conversation's life. A home
switch means evict + rebuild (`/new` semantics), never a hot swap inside a live turn.

## 2. Build slices (each: code + behaviour-contract tests, one commit per slice)

1. `gateway/user_profiles.py` — name derivation (`tg-<id>`), creation
   (`create_profile(no_skills=True)` + owner line in `.env` + SOUL.md copy + personal-dir
   migration + rescan fire), idempotency, `profile_exists` gate. Tests: derivation,
   creation contents/markers/permissions, re-create is a no-op, invalid ids rejected.
2. `resolve_identity` DM rule (D2) — `gateway/session_identity.py`, after the route match.
   Tests: profiled DM → `tg-<id>` runtime; unprofiled DM → launch home; group from the same
   user → launch home; owner DM → launch home; mapped-but-missing dir → dropped.
3. `_multiuser_home` → authorization home (D5) + audit of every `store_for_home` /
   `enabled` / `owner_user_id` call site that can receive a runtime home. Tests: admission
   for a profiled DM is answered by the launch store; the user home's store is never
   consulted for admission.
4. Creation triggers (D3) — grant confirm handler (`telegram_controls.py:848-900`) +
   `/allow` path (`multiuser.py`) + lazy backfill on first admitted DM. Tests: grant for a
   started user creates the profile; re-allow idempotent; `/start` alone creates nothing;
   group-scope grant creates nothing; banned user creates nothing; owner creates nothing.
5. Adapter fall-through (D7) — tenant-dispatch site branches on the pinned identity for
   profiled DMs; legacy-path command audit (profile vs host scope). Tests: profiled DM text
   → normal turn (not tenant runtime); unprofiled admitted DM → tenant runtime; group from
   a profiled user → tenant runtime (unchanged); `/guestbot` still adapter-side.
6. Provider fallback (D6) — profiled turns resolve the provider from the owner's scope.
   Tests: profiled turn gets a provider with an empty user `.env`; no owner keys land in
   the user `.env` or the user turn's secret scope.
7. Memory/skills migration (D8, D11) — creation-time copy + launch copies deleted +
   personal context re-homed. Tests: personal dir contents arrive intact; launch copies
   gone; `get_memory_dir()` under a profiled turn points inside the user home; fail-closed
   refusal preserved.
8. Sessions (D9) — new namespace, agent eviction, history migration (or documented fresh
   start). Tests: new key namespace; old cached agent evicted; history present (or the
   fresh-start decision recorded with the old history intact).
9. Browser strips (D10) + cron enumeration (D12). Tests: generated config has no absolute
   `user_data_dir`, `use_real_profile` false; a user home is ticked by the host ticker.
10. Neighbour sweep: affected + neighbour test files, `ruff check`, `git diff --check`.

## 3. Open questions (for the parent / user — not blocking the build)

1. **Session-history migration format (D9) — RESOLVED in slice 8:** lazy seed on the first
   profiled turn (last 40 tenant rows, normalized to `{role, content}`), once per user home
   ever (marker file), launch store never written.
2. **Provider model (D6, decided as owner's scope):** confirm the user is happy that admitted
   users keep burning his provider quota in their own profiles (same as today via the tenant
   runtime), and that user-supplied keys wait for a provisioning UI.
3. **SOUL.md copy:** the build copies the launch SOUL.md into the new profile (one bot, one
   persona). If the owner's SOUL.md ever names the owner personally, that copy is wrong —
   flag if he wants the default SOUL.md instead.
4. **`_SHADOW_CHECKED` growth (D11):** process-global, unkeyed by home, never evicts. With
   one home per user it grows unboundedly. Watch; a home-keyed variant is a separate fix.
5. **Scale:** `profiles_to_serve` / cron / `hermes update` enumerate every served home.
   Tens of user profiles are fine; hundreds turn those enumerations fleet-sized. No action
   now; revisit if the user count grows.
6. **Allowgc interplay — RESOLVED during the rebase onto main `bf0b8ef54b`:** allowgc's
   group-scope grants go through `grant_scope_access`, never `allow_user(True)`, so they
   never fire the creation triggers; the adapter backfill is DM-gated and a group-scope
   grant does not admit a DM. The DM re-approval path (`hm:allow:confirm`, which clears
   an `/imout` opt-out) does call `allow_user(True)` and correctly creates a real
   personal profile — exactly the specified rejoin flow.
