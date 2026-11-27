"""Recover heartbeat / scheduled-wakeup idle watches from the gateway's canonical persisted routing index."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("gateway.run")


def _scope_source_for_entry(runner, store, entry, source, watched_homes):
    """``(scope_key, source)`` for *entry*: the session key's namespace profile when it
    names a served profile, else the source-derived scope.

    The key namespace is the ground truth for where the session's rows live (the session
    row and its wakeup rows sit in that profile's store). A persisted origin can carry no
    profile — at ingress an allowgc group's runtime lived only in the wire-invisible
    identity, which never persists — so resolving the scope from the origin alone can land
    the pending check in the launch store, where it finds nothing and the wakeup never
    fires, silently: the session is already in ``scanned`` so the orphan sweep skips it
    too. The stamped copy (identity cleared, so a stale launch-home pin from
    ``restore_identity`` cannot win) is what gets registered, so the poll scopes the
    turn under the owning profile as well."""
    scope_key = runner._profile_scope_key_for_source(source)
    key_home = store._profile_home_for_key(entry.session_key)
    if key_home is None or scope_key == key_home:
        return scope_key, source
    watched = {str(Path(home)) for home in watched_homes}
    if str(key_home) not in watched:
        return scope_key, source
    key_profile = store._named_profile_for_key(entry.session_key)
    if key_profile is None:
        return scope_key, source
    from gateway.session_identity import clear_identity, replace_source

    stamped = replace_source(source)
    clear_identity(stamped)
    stamped.profile = key_profile
    return key_home, stamped


def _warn_orphan_wakeup(runner, session_id: str, reason: str, count: int | None = None) -> None:
    """One WARNING per gateway process for a wakeup row with no deliverable route.

    A row whose session the routing index no longer lists (a /new or a reset replaced the
    route) can never fire through the restore scan; without this it sits silent forever.
    The warning carries the pending count so the owner can judge it from the log alone.
    ``count`` is the probe-read pending count; when omitted it is re-read under the
    ambient scope (only correct when that scope is the session's own)."""
    warned = getattr(runner, "_warned_orphan_wakeup_sessions", None)
    if warned is None:
        warned = runner._warned_orphan_wakeup_sessions = set()
    if session_id in warned:
        return
    warned.add(session_id)
    if count is None:
        try:
            from hermes_cli.wakeups import load_wakeups

            count = len(load_wakeups(session_id).pending)
        except Exception:
            logger.debug("wakeup orphan count unreadable for %s", session_id, exc_info=True)
            count = 0
    logger.warning(
        "wakeup orphan: session %s holds %d pending wakeup(s) but %s; the wakeup(s) cannot "
        "fire until the session is active again",
        session_id, count, reason,
    )


def _warn_unreadable_wakeup_store(runner, home) -> None:
    """One WARNING per gateway process when a served profile's wakeup store cannot be read
    at all: without it a store the probe cannot open hides its rows forever."""
    warned = getattr(runner, "_warned_unreadable_wakeup_stores", None)
    if warned is None:
        warned = runner._warned_unreadable_wakeup_stores = set()
    key = str(home)
    if key in warned:
        return
    warned.add(key)
    logger.warning(
        "wakeup sweep cannot read %s's wakeup store; pending wakeups there cannot fire",
        home,
    )


def _profile_wakeup_rows(runner, home):
    """``{session_id: pending-count}`` for *home*'s store, or None when the store cannot
    be read. Reads through the lightweight session-db probe (home override only): the
    sweep needs the rows, never the profile's secrets or terminal scope, so a scope
    that cannot be entered must not hide them. A sid whose count reads back 0 was
    claimed between the list and the read — it is no longer pending and is dropped."""
    from gateway.run_idle_gates import _profile_session_db_probe
    from hermes_cli.wakeups import list_sessions_with_wakeups_in, load_wakeups_in

    db = _profile_session_db_probe(home)
    if db is None:
        return None
    try:
        sids = list_sessions_with_wakeups_in(db)
    except Exception:
        # A store this probe cannot read is unreadable, whatever the cause; the
        # once-per-process WARNING upstream carries the signal, this is the detail.
        logger.debug("wakeup sweep read failed for %s", home, exc_info=True)
        return None
    return {sid: len(load_wakeups_in(db, sid).pending) for sid in sids if sid}


def _watched_homes(runner, default_home) -> list:
    """Every home the sweep's ``_profile_scope_for_source`` can resolve an origin to: the gateway home
    plus, under multiplex, the whole served set INCLUDING ``default`` — a ``-p work`` multiplexer's own
    home is not ``~/.hermes``, so dropping ``default`` here would hide its heartbeats forever."""
    from gateway.run import _multiplex_profile_homes

    homes = [default_home]
    if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        homes += [home for _name, home in _multiplex_profile_homes(runner.config)]
    return list(dict.fromkeys(Path(home) for home in homes))


async def restore_heartbeat_watches(runner) -> None:
    """Retryable startup/poll scan; failed reads never prune existing watches.

    SessionStore owns one routing index across profiles. Its origin and exact key,
    rather than a second heartbeat routing snapshot, also cover pre-upgrade state.
    Run all storage work off-loop so a cold profile DB cannot block adapters.
    """
    from gateway.run import _profile_runtime_scope
    from gateway.run_idle_gates import profile_has_active_heartbeat, profile_has_pending_wakeup
    from hermes_cli.heartbeat import HeartbeatManager
    from hermes_cli.wakeups import WakeupManager
    from hermes_constants import get_hermes_home

    store = runner.session_store

    def scan():
        restored = []
        # The poller may have been spawned by a named profile's /heartbeat command.
        # Anchor even default origins to the gateway home, not inherited context.
        home = getattr(store, "_routing_home", None) or get_hermes_home()
        # Cheap gate: with no heartbeat or pending wakeup persisted in any served profile there is
        # nothing to restore — skip the per-origin profile-scope re-parse over every routed session.
        watched = _watched_homes(runner, home)
        if not any(profile_has_active_heartbeat(h) or profile_has_pending_wakeup(h) for h in watched):
            return restored
        with _profile_runtime_scope(home):
            # Enter each profile's scope once per scan, not once per routed session: a scope entry
            # hydrates the secret scope and terminal policy, so N sessions cost N parses otherwise.
            # Sources are read through _restored_source so the persisted receiving bot is re-pinned
            # before the scope key is derived; the same source object is what gets registered.
            by_scope: dict = {}
            scanned: set = set()
            for entry in store.list_sessions():
                if entry.origin is None or not entry.session_id or entry.suspended:
                    continue
                scanned.add(entry.session_id)
                try:
                    source = runner._restored_source(entry)
                    scope_key, grouped = _scope_source_for_entry(
                        runner, store, entry, source, watched)
                    by_scope.setdefault(scope_key, []).append((entry, grouped))
                except Exception:
                    logger.debug("heartbeat restore for %s failed", entry.session_key, exc_info=True)
            for group in by_scope.values():
                try:
                    with runner._profile_scope_for_source(group[0][1]):
                        for entry, source in group:
                            try:
                                # Agent-scheduled wakeups ride the same idle watch as /heartbeat.
                                if HeartbeatManager(entry.session_id).is_active() or WakeupManager(entry.session_id).has_pending():
                                    restored.append((entry.session_key, source, entry.session_id))
                            except Exception:
                                logger.debug("heartbeat restore for %s failed", entry.session_key, exc_info=True)
                except Exception:
                    logger.debug("heartbeat restore scope for %s failed", group[0][0].session_key, exc_info=True)
            # A wakeup row whose session never got a watch — no routing entry, or an entry
            # the loop saw but did not register — must surface instead of sitting silent
            # forever. The sweep reads each home's rows through the probe (see
            # _profile_wakeup_rows), so a scope that cannot be entered hides nothing.
            registered = {sid for _key, _source, sid in restored}
            for watched_home in watched:
                rows = _profile_wakeup_rows(runner, watched_home)
                if rows is None:
                    _warn_unreadable_wakeup_store(runner, watched_home)
                    continue
                for sid, count in rows.items():
                    if count <= 0 or sid in registered:
                        continue
                    entry = store.lookup_by_session_id(sid)
                    if entry is None:
                        reason = "has no routing entry"
                    elif entry.suspended:
                        reason = "is suspended"
                    elif sid in scanned:
                        reason = "no watch was registered for it"
                    else:
                        reason = "has no deliverable origin"
                    _warn_orphan_wakeup(runner, sid, reason, count=count)
        return restored

    try:
        candidates = await runner._run_in_executor_with_context(scan)
        for key, source, session_id in candidates:
            # A reset/compression may have published a new owner during the executor hop.
            if store.peek_session_id(key) == session_id:
                runner._register_heartbeat_watch(key, source, session_id)
    except Exception:
        logger.debug("heartbeat restore scan failed; retrying on next poll", exc_info=True)
