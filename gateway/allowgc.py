"""One shared temporary group profile per Telegram group (``/allowgc``).

When the owner enables ``/allowgc`` in a group, every member of that group
runs Hermes inside a single group profile (``gc-<chat id>``) with a shared
session: the group session key drops the user segment, so the whole group
shares one conversation, one memory, one config. The profile only accepts
messages from that group, never DM and never any other chat.

The group profile is a routed satellite: it never connects an adapter
itself (two profiles cannot share one bot credential — the token lock), it
only receives messages the launch profile's adapter routes to it.

Disabling preserves the profile for seven days (``ALLOWGC_PRESERVE_DAYS``)
so the owner can re-enable without losing anything; after that the profile
directory is reaped on the next lookup.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ALLOWGC_STATE_FILENAME = "allowgc.json"
ALLOWGC_PROFILE_PREFIX = "gc-"
ALLOWGC_PRESERVE_DAYS = 7
ALLOWGC_CONFIRM_SECONDS = 6 * 60 * 60  # same 6h window as the allow-confirm fix

TOGGLE_NAMES = ("tools", "web", "shell", "trigger")
DEFAULT_TOGGLES = {"tools": True, "web": True, "shell": True, "trigger": True}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def profile_name_for_chat(chat_id: Any) -> str:
    """Group profile name for a Telegram chat id (``gc-<abs(chat id)>``)."""
    return f"{ALLOWGC_PROFILE_PREFIX}{abs(int(chat_id))}"


def state_path(home: Path | str) -> Path:
    return Path(home) / ALLOWGC_STATE_FILENAME


def load_state(home: Path | str) -> dict[str, Any] | None:
    """Read the allowgc state file for a profile home; None when absent/invalid."""
    try:
        raw = Path(home, ALLOWGC_STATE_FILENAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_state(home: Path | str, state: dict[str, Any]) -> None:
    """Atomically write the allowgc state file (mkstemp + os.replace)."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{ALLOWGC_STATE_FILENAME}.", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, home / ALLOWGC_STATE_FILENAME)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def new_state(chat_id: Any, chat_name: str | None, owner_id: Any,
              toggles: dict[str, bool] | None = None) -> dict[str, Any]:
    """Fresh state dict for a newly enabled group."""
    merged = dict(DEFAULT_TOGGLES)
    if toggles:
        for name in TOGGLE_NAMES:
            if name in toggles:
                merged[name] = bool(toggles[name])
    return {
        "version": 1,
        "chat_id": str(chat_id),
        "chat_name": chat_name or "",
        "enabled": True,
        "created_at": _utcnow_iso(),
        "disabled_at": None,
        "owner_id": str(owner_id),
        "toggles": merged,
        "opted_out": [],
        "card_message_id": None,
    }


def _home_for_chat(chat_id: Any) -> Path | None:
    from hermes_cli.profiles import get_profile_dir, profile_exists

    name = profile_name_for_chat(chat_id)
    if not profile_exists(name):
        return None
    return get_profile_dir(name)


def group_home_for_source(source: Any) -> Path | None:
    """The active allowgc group profile home for this source, else None."""
    if getattr(source, "chat_type", "") not in {"group", "forum"}:
        return None
    try:
        return resolve_home(source.chat_id, reap=False)
    except Exception:
        import logging

        logging.getLogger(__name__).debug("allowgc group home lookup failed", exc_info=True)
        return None


def resolve_home(chat_id: Any, *, reap: bool = True) -> Path | None:
    """Return the group profile home when allowgc is enabled for the chat.

    Returns None when there is no group profile, when it is disabled, or
    when the state file is missing/invalid. A disabled profile older than
    ``ALLOWGC_PRESERVE_DAYS`` is reaped (directory removed) on lookup.
    """
    home = _home_for_chat(chat_id)
    if home is None:
        return None
    state = load_state(home)
    if state is None:
        return None
    if state.get("enabled"):
        return home
    if not reap:
        return None
    disabled_at = _parse_iso(state.get("disabled_at"))
    if disabled_at is None:
        return None
    if _utcnow() - disabled_at >= timedelta(days=ALLOWGC_PRESERVE_DAYS):
        # Expired preservation window: remove the whole group profile.
        # Guard the rmtree on the gc- prefix and the state file so a
        # misresolved home can never delete an unrelated profile.
        if home.name.startswith(ALLOWGC_PROFILE_PREFIX) and state_path(home).exists():
            shutil.rmtree(home, ignore_errors=True)
    return None


def is_active_for_chat(chat_id: Any) -> bool:
    """True when allowgc is enabled for this chat right now (no reaping)."""
    return resolve_home(chat_id, reap=False) is not None


def owner_id_for_chat(chat_id: Any) -> str | None:
    """The real owner id recorded for an active group profile, else None."""
    home = resolve_home(chat_id, reap=False)
    if home is None:
        return None
    state = load_state(home)
    if not state:
        return None
    owner = state.get("owner_id")
    return str(owner) if owner else None


def _platform_value(source: Any) -> str:
    platform = getattr(source, "platform", None)
    value = getattr(platform, "value", None)
    if isinstance(value, str):
        return value.lower()
    return str(platform or "").lower()


def is_allowgc_source(source: Any) -> bool:
    """True when a session source is a Telegram group/forum with allowgc on."""
    if _platform_value(source) != "telegram":
        return False
    if getattr(source, "chat_type", None) not in {"group", "forum"}:
        return False
    chat_id = getattr(source, "chat_id", None)
    if chat_id is None:
        return False
    return is_active_for_chat(chat_id)


def shared_session_for_source(source: Any) -> bool:
    """True when this source's session key must drop the user segment."""
    return is_allowgc_source(source)


def _set_env_key(env_path: Path, key: str, value: str) -> None:
    """Set ``KEY=value`` in a .env file (replace in place, else append)."""
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    needle = f"{key}="
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(needle):
            lines[index] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(env_path, 0o600)


def create_group_profile(chat_id: Any, chat_name: str | None, owner_id: Any,
                         *, toggles: dict[str, bool] | None = None) -> Path:
    """Create (or re-enable) the shared group profile for a chat.

    Idempotent: a second call re-enables an existing profile instead of
    failing, so a double-tapped Confirm still ends in the enabled card.
    The profile's owner is the real owner (``OWNER_TELEGRAM_ID`` in its
    .env), which is what makes group-level bans owner-only: the tenant
    store's owner check compares against this id.
    """
    from hermes_cli.profiles import create_profile, get_profile_dir, profile_exists

    name = profile_name_for_chat(chat_id)
    if profile_exists(name):
        home = get_profile_dir(name)
        state = load_state(home)
        if state is None:
            state = new_state(chat_id, chat_name, owner_id, toggles)
        state["enabled"] = True
        state["disabled_at"] = None
        if toggles:
            for toggle_name in TOGGLE_NAMES:
                if toggle_name in toggles:
                    state.setdefault("toggles", {})[toggle_name] = bool(toggles[toggle_name])
        save_state(home, state)
        return home

    home = create_profile(
        name, no_skills=True,
        description=f"Temporary shared profile for Telegram group {chat_name or ''} ({chat_id}).",
    )
    _set_env_key(home / ".env", "OWNER_TELEGRAM_ID", str(owner_id))
    save_state(home, new_state(chat_id, chat_name, owner_id, toggles))
    return home


def disable_group_profile(chat_id: Any) -> dict[str, Any] | None:
    """Disable allowgc for a chat, preserving the profile for seven days.

    Returns the updated state, or None when there is no active profile.
    """
    home = resolve_home(chat_id, reap=False)
    if home is None:
        return None
    state = load_state(home)
    if state is None:
        return None
    state["enabled"] = False
    state["disabled_at"] = _utcnow_iso()
    save_state(home, state)
    return state


def set_toggle(home: Path | str, name: str, value: bool) -> dict[str, Any] | None:
    """Set one access toggle on a group profile's state; None when no state."""
    if name not in TOGGLE_NAMES:
        raise ValueError(f"Unknown allowgc toggle: {name}")
    state = load_state(home)
    if state is None:
        return None
    state.setdefault("toggles", {})[name] = bool(value)
    save_state(home, state)
    return state


def card_message_id_for_chat(chat_id: Any) -> str | None:
    """The stored /allowgc card message id for a chat, if any.

    A repeat /allowgc edits this message in place instead of sending a
    new card. Only a decimal id is ever returned; anything else means no
    usable card is stored.
    """
    home = _home_for_chat(chat_id)
    if home is None:
        return None
    state = load_state(home)
    if not state:
        return None
    value = state.get("card_message_id")
    return str(value) if str(value or "").isdecimal() else None


def set_card_message_id(home: Path | str, message_id: Any) -> dict[str, Any] | None:
    """Record the /allowgc card message id on a group profile's state."""
    state = load_state(home)
    if state is None:
        return None
    state["card_message_id"] = str(message_id)
    save_state(home, state)
    return state


def toggles_for_chat(chat_id: Any) -> dict[str, bool]:
    """Current toggles for an active group profile (defaults when none)."""
    home = resolve_home(chat_id, reap=False)
    if home is None:
        return dict(DEFAULT_TOGGLES)
    state = load_state(home)
    if not state:
        return dict(DEFAULT_TOGGLES)
    merged = dict(DEFAULT_TOGGLES)
    merged.update({k: bool(v) for k, v in (state.get("toggles") or {}).items()
                   if k in TOGGLE_NAMES})
    return merged


def preserved_toggles_for_chat(chat_id: Any) -> dict[str, bool] | None:
    """Toggles recorded in a group profile's state file; None when none.

    Turn-off preserves the access toggles in the state file; a repeat
    /allowgc seeds the fresh confirmation card from them so re-enabling
    keeps what was there instead of resetting to the defaults. Unlike
    ``toggles_for_chat`` this reads the file even when the profile is
    disabled — that is the point: the preserved values are what a
    re-enable must show.
    """
    home = _home_for_chat(chat_id)
    if home is None:
        return None
    state = load_state(home)
    if not state:
        return None
    merged = dict(DEFAULT_TOGGLES)
    merged.update({k: bool(v) for k, v in (state.get("toggles") or {}).items()
                   if k in TOGGLE_NAMES})
    return merged


def filter_turn_toolsets(toolsets: list[str] | None, source: Any) -> list[str]:
    """Apply the allowgc access toggles to a normal turn's toolsets.

    Only applies to member turns inside an active allowgc group; the real
    owner keeps full access, and every other source passes through
    unchanged. Tools off removes every toolset; Shell off drops the
    terminal; Web off drops the web and browser toolsets.
    """
    if not toolsets:
        return list(toolsets or [])
    if not is_allowgc_source(source):
        return list(toolsets)
    owner_id = owner_id_for_chat(getattr(source, "chat_id", None))
    if owner_id is not None and str(getattr(source, "user_id", "") or "") == owner_id:
        return list(toolsets)
    toggles = toggles_for_chat(getattr(source, "chat_id", None))
    if not toggles.get("tools", True):
        return []
    filtered = list(toolsets)
    if not toggles.get("shell", True):
        filtered = [name for name in filtered if name != "terminal"]
    if not toggles.get("web", True):
        filtered = [name for name in filtered if name not in {"web", "browser"}]
    return filtered


def is_opted_out(home: Path | str, user_id: Any) -> bool:
    state = load_state(home)
    if not state:
        return False
    return str(user_id) in {str(item) for item in (state.get("opted_out") or [])}


def opt_out(home: Path | str, user_id: Any) -> dict[str, Any] | None:
    """Drop a user id from the allowgc profile (their /imout opt-out).

    The removal is wired, not just listed: the member's grants in the
    group store are revoked as well, so every reader of the store (the
    adapter's authorization, the runner gate, the tenant paths) sees them
    as not allowed even if it never consults the opt-out list — and the
    list entry itself blocks re-auto-admission on their next group
    message. A store failure never loses the list entry: it is recorded
    first and the revoke is best-effort (warned, not raised).
    """
    state = load_state(home)
    if state is None:
        return None
    opted = {str(item) for item in (state.get("opted_out") or [])}
    opted.add(str(user_id))
    state["opted_out"] = sorted(opted)
    save_state(home, state)
    _revoke_group_grant(home, state, user_id)
    return state


def _revoke_group_grant(home: Path | str, state: dict[str, Any], user_id: Any) -> None:
    """Best-effort: revoke one member's grants in the group store.

    The group store is the admission readers consult; leaving a scope
    grant live after opt-out would keep admitting the member on every
    path that reads the store without checking the opt-out list. Only a
    live grant is touched, and only by the recorded group owner.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from gateway.mongo_multiuser import principal, store_for_home

        owner_id = state.get("owner_id")
        chat_id = state.get("chat_id")
        if not owner_id or not chat_id:
            return
        store = store_for_home(home)
        access_for_scope = getattr(store, "access_for_scope", None)
        if not callable(access_for_scope):
            return
        if not access_for_scope(str(user_id), "group", str(chat_id)).get("allowed"):
            return
        store.allow_user(principal(str(owner_id)), str(user_id), False)
    except Exception:
        logger.warning(
            "Could not revoke Telegram allowgc grant for user %s; opt-out list still applies",
            user_id, exc_info=True,
        )


def clear_opt_outs_for_user(user_id: Any) -> list[Path]:
    """Re-admit a user everywhere: remove their id from every gc profile.

    Used when a user re-joins via /start in DM after /imout. Returns the
    profiles that were changed.
    """
    from hermes_cli.profiles import list_profiles

    changed: list[Path] = []
    try:
        profiles = list_profiles()
    except Exception:
        return changed
    for profile in profiles:
        name = getattr(profile, "name", None)
        if not isinstance(name, str) or not name.startswith(ALLOWGC_PROFILE_PREFIX):
            continue
        home = resolve_home(_chat_id_from_profile_name(name), reap=False)
        if home is None:
            continue
        state = load_state(home)
        if not state:
            continue
        opted = {str(item) for item in (state.get("opted_out") or [])}
        if str(user_id) not in opted:
            continue
        opted.discard(str(user_id))
        state["opted_out"] = sorted(opted)
        save_state(home, state)
        changed.append(home)
    return changed


def _chat_id_from_profile_name(name: str) -> str:
    """Recover the (absolute) chat id a gc- profile name was built from."""
    return name[len(ALLOWGC_PROFILE_PREFIX):]
