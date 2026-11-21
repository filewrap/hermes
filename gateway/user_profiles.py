"""Universal profiles: one Hermes home per admitted Telegram user.

A profile means an allowed user or the owner who has started the bot and holds
access/authority to handle Hermes. Each such user gets their own home directory
(``profiles/tg-<telegram user id>/``); everything they own, run, make, edit, or add
lives there. The shared bot, its allowlist, and group routing do not change.

Design: ``docs/plans/universal-profiles-design.md``. The routing rule lives here as
:func:`resolve_user_profile` and is applied by
``gateway.session_identity.resolve_identity`` after the explicit ``profile_routes``
match; profile creation is triggered by the owner's grant (see
:func:`ensure_user_profile`) and by a lazy backfill on the first admitted DM turn.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Profile names for Telegram users are derived, never stored: the name IS the mapping,
# so no mapping table can drift from the directories on disk. Keyed by the numeric
# Telegram user id (usernames change; ids don't). Never "main" (see
# gateway/session.py::_session_key_namespace).
USER_PROFILE_PREFIX = "tg-"


# Commands that stay tied to the LAUNCH owner and must never run in a
# profiled user's normal turn (D7). The no-limits rework deleted the old
# TENANT_OWNER_ONLY_COMMANDS set, so the host scope is stated explicitly
# here instead of derived from a tenant policy: gateway lifecycle, admission,
# global security policy, and host diagnostics. Profile-scoped owner
# commands (the user's own skills, memory, model settings, their own run)
# are NOT here — they act on the user's own home. The Telegram client/bot
# operation commands and the ``goal gate add`` subcommand are host-scoped
# too; they are checked in the adapter alongside this set.
HOST_SCOPED_COMMANDS = frozenset({
    "allow", "allowlist", "approve", "approvals", "debug",
    "deny", "disallow", "egress", "guestbot", "login",
    "pause", "platform", "reload-mcp", "reload-skills",
    "restart", "sethome", "sync", "thread", "topic", "topup",
    "update", "yolo", "auto",
    "logs", "hf", "webauth",
})


TENANT_HISTORY_SEED_MARKER = ".tenant-history-seeded"


def tenant_history_seed(store, user_id, chat_id, thread_id, launch_home) -> list[dict]:
    """Normalized tenant history rows for a profiled DM's first turn, or [].

    Reads the launch store's ``history:<room>`` record — the same key
    ``TenantRuntime`` writes (``room_key`` with profile = resolved launch
    home) — validates each row against this user/chat/thread, and returns the
    last 40 as plain ``{role, content}`` rows the gateway turn can replay.
    Never raises: an unreadable store or a malformed record seeds nothing.
    The launch store is never written.
    """
    try:
        from gateway.mongo_multiuser import principal
        from gateway.tenant_runtime import room_key

        profile_key = str(Path(launch_home).expanduser().resolve(strict=False))
        key = room_key(user_id, chat_id, thread_id, profile=profile_key)
        rows = store.get(principal(user_id), "profile", "history:" + key) or []
    except Exception:
        logger.warning("Universal profiles: tenant-history seed unreadable", exc_info=True)
        return []
    if not isinstance(rows, list):
        return []
    seed: list[dict] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("role") not in {"user", "assistant"}
            or not isinstance(row.get("content"), str)
            or str(row.get("user_id")) != str(user_id)
            or str(row.get("chat_id")) != str(chat_id)
            or row.get("thread_id") != thread_id
            or str(row.get("profile")) != profile_key
        ):
            return []
        seed.append({"role": row["role"], "content": row["content"]})
    return seed[-40:]


def maybe_seed_profiled_turn_history(source, launch_home, user_home) -> list[dict]:
    """Tenant-history seed for a profiled DM's first turn, or [].

    Applies once per user home ever (marker file): a later session reset must
    not resurrect history the user deliberately cleared. The seed is read live
    from the launch store, so it includes the backfill turn's rows (that turn
    still ran under the tenant runtime). The launch store is never written —
    the old history stays preserved read-only there, and a tombstone fallback
    routes back to it. Never raises.
    """
    if not is_profiled_dm_source(source):
        return []
    user_home = Path(user_home)
    marker = user_home / TENANT_HISTORY_SEED_MARKER
    if marker.exists():
        return []
    try:
        from gateway.mongo_multiuser import store_for_home

        store = store_for_home(launch_home)
        seed = tenant_history_seed(
            store, getattr(source, "user_id", None), getattr(source, "chat_id", None),
            getattr(source, "thread_id", None), launch_home,
        )
    except Exception:
        logger.warning("Universal profiles: tenant-history seed unavailable", exc_info=True)
        return []
    try:
        marker.write_text("seeded\n", encoding="utf-8")
    except OSError:
        # Fail closed: without the marker a later reset would resurrect the
        # seeded history, so seed nothing instead of seeding unmarked.
        logger.warning("Universal profiles: could not write the history-seed marker in %s", user_home)
        return []
    return seed


def user_profile_name(user_id: Any) -> Optional[str]:
    """``tg-<id>`` for a numeric Telegram user id, else None.

    Accepts ``int`` or a decimal string; rejects zero, negatives, blanks, and
    anything that is not a plain decimal id (usernames are display-only and must
    never become a profile name).
    """
    if isinstance(user_id, bool):
        return None
    if isinstance(user_id, int):
        return f"{USER_PROFILE_PREFIX}{user_id}" if user_id > 0 else None
    text = str(user_id or "").strip()
    if not text.isdigit() or int(text) <= 0:
        return None
    return f"{USER_PROFILE_PREFIX}{int(text)}"


def is_user_profile_name(name: Any) -> bool:
    """True when *name* is a derived per-user profile name (``tg-<numeric id>``)."""
    if not isinstance(name, str) or not name.startswith(USER_PROFILE_PREFIX):
        return False
    return user_profile_name(name[len(USER_PROFILE_PREFIX):]) == name


def _platform_is_telegram(source: Any) -> bool:
    platform = getattr(source, "platform", None)
    value = getattr(platform, "value", platform)
    return str(value or "").lower() == "telegram"


def _owner_user_id(authorization_home: Path) -> Optional[str]:
    """The owner id configured in *authorization_home*'s ``.env``, else None.

    Never raises: an unreadable owner means "do not route" (fail closed to the
    launch home), never "route anyway".
    """
    try:
        from gateway.mongo_multiuser import owner_user_id

        owner = owner_user_id(authorization_home)
    except Exception:
        logger.debug("Universal profiles: owner identity unavailable for %s", authorization_home, exc_info=True)
        return None
    owner = str(owner or "").strip()
    return owner if owner.isdigit() and int(owner) > 0 else None


def resolve_user_profile(
    source: Any,
    *,
    transport_name: str,
    primary_profile: str,
    authorization_home: Path,
) -> Optional[str]:
    """Per-user runtime profile name for an inbound *source*, else None.

    Applies only to a Telegram DM on the primary (shared) bot, from a non-bot
    sender whose per-user home already exists. Explicit ``profile_routes`` entries
    win (the caller applies this only when no route matched); a secondary bot's
    own DM never takes this branch; the owner keeps the launch home (their
    profile); groups, forums, channels, and non-Telegram platforms never route
    by sender. An unmapped user (no home yet) returns None and keeps today's
    behaviour — the adapter's admission gate drops them silently as before.
    """
    if transport_name != primary_profile:
        return None
    if not _platform_is_telegram(source):
        return None
    if getattr(source, "chat_type", None) != "dm":
        return None
    if getattr(source, "is_bot", False):
        return None
    name = user_profile_name(getattr(source, "user_id", None))
    if name is None:
        return None
    from hermes_cli.profiles import profile_exists

    if not profile_exists(name):
        return None
    owner = _owner_user_id(authorization_home)
    if owner is None or str(getattr(source, "user_id", "")) == owner:
        return None
    return name


def _personal_dir(home: Path, user_id: str) -> Path:
    """``<home>/personal/telegram/users/<uid>`` (the settings file's parent)."""
    from gateway.telegram_personal_settings import personal_settings_path

    return personal_settings_path(home, user_id).parent


def migrate_personal_dir(user_id: str, *, launch_home: Path, user_home: Path) -> bool:
    """Move one user's personal overlay out of the launch home into their profile.

    Copies ``<launch>/personal/telegram/users/<uid>/`` (settings.json, memory/,
    skills/) into the same relative path under *user_home*, verifies every file
    arrived, then deletes the launch copy. The user's data must have exactly one
    live source. Copy, never symlink: the personal-settings path checks reject
    symlinks and paths escaping the home. Returns False (leaving the launch copy
    intact) when there is nothing to move or the move fails.
    """
    try:
        src = _personal_dir(launch_home, user_id)
    except ValueError:
        logger.warning("Universal profiles: refusing personal-dir migration for invalid user id %r", user_id)
        return False
    if not src.is_dir():
        return True
    try:
        dst = _personal_dir(user_home, user_id)
    except ValueError:
        logger.warning("Universal profiles: refusing personal-dir migration into invalid home %s", user_home)
        return False
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        expected = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
        missing = [rel for rel in expected if not (dst / rel).is_file()]
        if missing:
            logger.warning(
                "Universal profiles: personal-dir migration for user %s incomplete (%d files missing); "
                "keeping the launch copy",
                user_id, len(missing),
            )
            return False
    except Exception:
        logger.warning("Universal profiles: personal-dir migration for user %s failed", user_id, exc_info=True)
        return False
    try:
        shutil.rmtree(src)
    except Exception:
        logger.warning("Universal profiles: migrated personal dir for user %s but could not remove the launch copy",
                       user_id, exc_info=True)
        return False
    return True


def _write_owner_env(user_home: Path, user_id: str) -> bool:
    """Append ``OWNER_TELEGRAM_ID=<user id>`` to the new home's ``.env``.

    The user is the owner of their own profile: without a configured owner the
    profile is "not ready" (multiuser disabled, owner reads raise) and an
    effectively empty ``.env`` does not count as a servable identity. Never copy
    the launch ``.env`` — it carries the owner's id and the owner's tokens.
    """
    env_path = user_home / ".env"
    try:
        existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    except OSError:
        logger.warning("Universal profiles: could not read %s", env_path, exc_info=True)
        return False
    if "OWNER_TELEGRAM_ID=" in existing:
        return True
    line = f"OWNER_TELEGRAM_ID={user_id}\n"
    try:
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        logger.warning("Universal profiles: could not write owner id into %s", env_path, exc_info=True)
        return False
    return True


def _copy_soul(launch_home: Path, user_home: Path) -> None:
    """Give the new profile the same bot persona as the launch home.

    One bot, one persona: the user talks to the same Hermes, not a default
    stranger. Best-effort — a missing launch SOUL.md leaves the default one
    ``create_profile`` wrote.
    """
    src = launch_home / "SOUL.md"
    if not src.is_file():
        return
    try:
        (user_home / "SOUL.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        logger.warning("Universal profiles: could not copy SOUL.md into %s", user_home, exc_info=True)


def _notify_multiplexer(name: str) -> None:
    """Ask the live gateway to serve the new profile now (best-effort)."""
    try:
        from hermes_cli.gateway_multiplex_served import notify_multiplexer_profiles_changed

        notify_multiplexer_profiles_changed(name)
    except Exception:
        logger.debug("Universal profiles: multiplexer notification failed for %r", name, exc_info=True)


def is_profiled_dm_source(source: Any) -> bool:
    """True when a Telegram DM source was routed to a per-user profile (D7):
    the identity pinned at canonicalize names a ``tg-<id>`` runtime profile.
    Groups never count; an unprofiled DM keeps the tenant runtime."""
    if source is None or getattr(source, "chat_type", None) != "dm":
        return False
    return is_user_profile_name(getattr(source, "profile", None))


def maybe_create_user_profile(store: Any, user_id: Any) -> Optional[Path]:
    """Create the per-user home after a global allow grant (or a backfill check).

    Fires only when the user is admitted AND started AND not banned AND not the
    owner — the exact trigger for a universal profile. The started gate uses
    ``resolve_started_user`` where the store exposes it (Local backend); a store
    without it (Mongo-primary) is gated on the allow alone. Idempotent; never
    raises — a failed creation must never break the grant that just succeeded
    (the turn runs under the launch home, as today, and the next grant or DM
    retries).
    """
    if store is None:
        return None
    try:
        uid = str(user_id or "").strip()
        if user_profile_name(uid) is None:
            return None
        if uid == str(getattr(store, "owner_user_id", "") or ""):
            return None
        access = store.get_access(uid)
        if not access.get("allowed") or access.get("banned"):
            return None
        resolve_started = getattr(store, "resolve_started_user", None)
        if callable(resolve_started) and resolve_started(uid) is None:
            return None
        launch_home = Path(store.home)
        return ensure_user_profile(uid, launch_home=launch_home)
    except Exception:
        logger.warning(
            "Universal profiles: could not create a profile for user %s after grant",
            user_id, exc_info=True,
        )
        return None


def _strip_seeded_model_config(home: Path) -> bool:
    """Remove the model block ``create_profile`` seeds into a FRESH user home.

    ``create_profile`` copies the launch profile's ``model`` block (provider,
    endpoint, and ``api_key`` when it lives in config.yaml) into every fresh
    profile so it is usable — for a per-user home that is an owner-key leak
    (D6: the user home starts with no provider keys; the owner's provider is
    resolved per turn under the owner's scope, never from the user home).
    Only ever called on a home ``create_profile`` just minted, never on an
    existing home, so a config.yaml the user has since written is untouched.
    The seed's migration backup (``backups/``) goes with it: a fresh home's
    backups can only be copies of the seed.
    """
    seeded = Path(home) / "config.yaml"
    try:
        seeded.unlink(missing_ok=True)
        backups = Path(home) / "backups"
        if backups.exists():
            shutil.rmtree(backups)
    except OSError:
        logger.error(
            "Universal profiles: could not strip the seeded config.yaml from %s; "
            "refusing to hand out a home carrying the owner's keys",
            home,
        )
        return False
    return True


def ensure_user_profile(user_id: Any, *, launch_home: Path) -> Optional[Path]:
    """Create (or find) the per-user home for *user_id*; None on failure.

    Idempotent: an existing home is returned as-is (a re-allow, a re-run, or a
    retry after a partial creation never mints a second home). A fresh home is
    an empty profile (``create_profile(no_skills=True)`` — no clone) plus the
    owner line in its ``.env``, the launch home's SOUL.md, and the user's
    personal overlay moved out of the launch home. ``create_profile`` also
    seeds the launch profile's model block into the fresh home; that seed is
    stripped here (D6 — the user home starts with no provider keys, so the
    owner's provider can only ever resolve under the owner's scope). The
    migration is retried on later calls when the launch copy still exists, so
    a partial creation self-heals.
    """
    uid = str(user_id or "").strip()
    name = user_profile_name(uid)
    if name is None:
        logger.warning("Universal profiles: refusing to create a profile for invalid user id %r", user_id)
        return None
    from hermes_cli.profiles import create_profile, get_profile_dir, profile_exists

    launch_home = Path(launch_home)
    if profile_exists(name):
        home = get_profile_dir(name)
        migrate_personal_dir(uid, launch_home=launch_home, user_home=home)
        return home
    try:
        home = create_profile(
            name, no_skills=True,
            description=f"Telegram user {uid} (universal profile)",
        )
    except Exception:
        logger.warning("Universal profiles: could not create profile %s for user %s", name, uid, exc_info=True)
        return None
    if not _strip_seeded_model_config(home):
        # Fail closed: a home carrying the owner's seeded keys must never be
        # routed to. Remove the half-created home so the next call retries.
        shutil.rmtree(home, ignore_errors=True)
        return None
    _write_owner_env(home, uid)
    _copy_soul(launch_home, home)
    migrate_personal_dir(uid, launch_home=launch_home, user_home=home)
    _notify_multiplexer(name)
    return home
