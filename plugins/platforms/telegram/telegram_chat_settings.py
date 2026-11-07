"""Profile-scoped Telegram chat access settings and their native inline UI."""
from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path
from typing import Any

from .mtproto_rich import inline_markup
from .rich_ui import _settings_pill, rich_html_table

_SETTINGS_LOCK = threading.RLock()
_GROUP_MODES = {"trigger", "tag", "direct"}


def parse_user_ids(value: Any) -> set[str]:
    """Normalize the allowlist formats accepted by config and environment."""
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        text = str(value).strip()
        if not text:
            return set()
        if text.startswith("["):
            try:
                import json
                decoded = json.loads(text)
                parts = decoded if isinstance(decoded, list) else [text]
            except (ValueError, TypeError):
                parts = text.split(",")
        else:
            parts = text.replace("\n", ",").split(",")
    return {str(item).strip().lstrip("@").casefold() for item in parts if str(item).strip()}


def _chat_record(home: str | Path, chat_type: str, chat_id: str) -> dict[str, Any]:
    from hermes_cli.config import read_user_config_raw

    config = read_user_config_raw(Path(home) / "config.yaml")
    platforms = config.get("platforms") if isinstance(config, dict) else None
    telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
    access = telegram.get("chat_access") if isinstance(telegram, dict) else None
    if not isinstance(access, dict):
        return {}
    if chat_type == "dm":
        record = access.get("direct_messages")
    else:
        groups = access.get("groups")
        record = groups.get(str(chat_id)) if isinstance(groups, dict) else None
    return dict(record) if isinstance(record, dict) else {}


def chat_policy_generation(home: str | Path, chat_type: str, chat_id: str) -> str:
    """Trusted lifecycle stamp, not a grant; never accept it from a chat/model.

    A fresh opaque revision on each write prevents OFF -> ON from reviving
    old guest turns, even when the resulting policy values are identical.
    Include values as well so a single external config edit is detected.
    """
    import hashlib
    import json

    record = _chat_record(home, chat_type, chat_id)
    revision = record.get("policy_revision", "legacy")
    if revision != "legacy" and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{32}", revision)):
        raise ValueError("Invalid Telegram chat policy revision")
    policy = {key: record.get(key) for key in ("trigger_word", "trigger_mode", "allowed_users", "blocked")}
    return hashlib.sha256(json.dumps([revision, policy], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_chat_settings(home: str | Path, chat_type: str, chat_id: str) -> dict[str, Any]:
    """Load the appropriate DM-wide or exact-group record; groups never share settings."""
    record = _chat_record(home, chat_type, chat_id)
    users = record.get("allowed_users")
    if isinstance(users, list):
        users = {str(user): {"id": str(user), "name": str(user)} for user in users}
    if not isinstance(users, dict):
        users = {}
    normalized_users: dict[str, dict[str, str]] = {}
    for user_id, detail in users.items():
        key = str(user_id).strip()
        if not key:
            continue
        if isinstance(detail, dict):
            normalized_users[key] = {
                "id": key,
                "name": _plain(detail.get("name") or detail.get("first_name") or key),
                "username": str(detail.get("username") or "").strip().lstrip("@"),
            }
        else:
            normalized_users[key] = {"id": key, "name": _plain(detail or key), "username": ""}
    mode = str(record.get("trigger_mode") or "").strip().lower()
    return {
        "trigger_word": str(record.get("trigger_word") or "").strip(),
        "trigger_mode": mode if mode in _GROUP_MODES else "",
        "allowed_users": normalized_users,
        "blocked": record.get("blocked") is True,
    }


def update_chat_settings(
    home: str | Path, chat_type: str, chat_id: str, changes: dict[str, Any],
) -> dict[str, Any]:
    """Atomically persist a small, profile-local settings update without touching secrets."""
    from hermes_cli.config import atomic_config_write, read_user_config_raw

    chat_type = str(chat_type or "").strip().lower()
    if chat_type not in {"dm", "private", "direct", "group", "forum", "channel"}:
        raise ValueError("Unsupported Telegram chat scope")
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Telegram chat settings must be a non-empty mapping")
    if set(changes) - {"trigger_word", "trigger_mode", "allowed_users", "blocked"}:
        raise ValueError("Unsupported Telegram chat setting")
    if "trigger_word" in changes:
        if chat_type not in {"group", "forum"} or type(changes["trigger_word"]) is not str:
            raise ValueError("Trigger word is a text group-only setting")
        word = changes["trigger_word"].strip()
        if word and (len(word) > 64 or re.search(r"\s", word)):
            raise ValueError("Trigger word must be a single word")
        changes = {**changes, "trigger_word": word}
    if "trigger_mode" in changes:
        if chat_type not in {"group", "forum", "channel"}:
            raise ValueError("Trigger mode is not a direct-message setting")
        mode = str(changes["trigger_mode"]).strip().lower()
        if mode not in _GROUP_MODES:
            raise ValueError("Telegram group trigger mode must be trigger, tag, or direct")
        changes = {**changes, "trigger_mode": mode}
    if "blocked" in changes:
        if chat_type not in {"group", "forum", "channel"} or type(changes["blocked"]) is not bool:
            raise ValueError("Blocked is a boolean group/channel-only setting")
    if "allowed_users" in changes:
        users = changes["allowed_users"]
        if not isinstance(users, dict) or len(users) > 500:
            raise ValueError("Telegram allowed users must be an object of at most 500 users")
        for user_id, detail in users.items():
            if not str(user_id).strip() or len(str(user_id)) > 32 or not isinstance(detail, (str, dict)):
                raise ValueError("Invalid Telegram allowed-user entry")
            if isinstance(detail, dict) and set(detail) - {"id", "name", "username"}:
                raise ValueError("Invalid Telegram allowed-user fields")
    with _SETTINGS_LOCK:
        config_path = Path(home) / "config.yaml"
        config = read_user_config_raw(config_path)
        if not isinstance(config, dict):
            raise ValueError("The active config.yaml is not a mapping")
        platforms = config.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            raise ValueError("The platforms section of config.yaml is not a mapping")
        telegram = platforms.setdefault("telegram", {})
        if not isinstance(telegram, dict):
            raise ValueError("The Telegram section of config.yaml is not a mapping")
        access = telegram.setdefault("chat_access", {})
        if not isinstance(access, dict):
            raise ValueError("The Telegram chat_access section is not a mapping")
        if chat_type == "dm":
            record = access.setdefault("direct_messages", {})
        else:
            groups = access.setdefault("groups", {})
            if not isinstance(groups, dict):
                raise ValueError("The Telegram chat_access.groups section is not a mapping")
            record = groups.setdefault(str(chat_id), {})
        if not isinstance(record, dict):
            raise ValueError("The Telegram chat access record is not a mapping")
        record.update(changes)
        # Opaque revisions also avoid lost-increment ABA when independent
        # config writers read the same old generation before their writes.
        record["policy_revision"] = secrets.token_hex(16)
        atomic_config_write(config_path, config)
    return load_chat_settings(home, chat_type, chat_id)


def upsert_allowed_user(
    home: str | Path, chat_type: str, chat_id: str, user: dict[str, str],
) -> dict[str, Any]:
    settings = load_chat_settings(home, chat_type, chat_id)
    users = settings["allowed_users"]
    user_id = str(user.get("id") or "").strip()
    if not user_id:
        raise ValueError("Telegram user ID is required")
    users[user_id] = {
        "id": user_id,
        "name": _plain(user.get("name") or user_id),
        "username": str(user.get("username") or "").strip().lstrip("@"),
    }
    return update_chat_settings(home, chat_type, chat_id, {"allowed_users": users})


def remove_allowed_user(
    home: str | Path, chat_type: str, chat_id: str, user_id: str,
) -> dict[str, Any]:
    settings = load_chat_settings(home, chat_type, chat_id)
    users = settings["allowed_users"]
    users.pop(str(user_id), None)
    return update_chat_settings(home, chat_type, chat_id, {"allowed_users": users})


def _plain(value: Any, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"[`*_\[\]<>]", "", text)
    return text[:limit]


def _html_text(value: Any, limit: int = 180) -> str:
    """Normalize and escape untrusted display text for native RichMessage HTML."""
    from html import escape

    text = " ".join(str(value or "").split())[:limit]
    return escape(text, quote=True)


def settings_view(
    chat_type: str,
    settings: dict[str, Any],
    *,
    chat_name: str = "",
    bot_name: str = "Hermes",
    bot_username: str = "",
    note: str = "",
    isolated: bool = False,
) -> tuple[str, Any]:
    """Render structured native RichMessage HTML and scope-appropriate controls."""
    is_dm = chat_type == "dm"
    is_group = chat_type in {"group", "forum"}
    if is_dm:
        title = "Direct message settings"
    elif chat_type == "channel":
        title = f"Channel settings · {chat_name or 'this chat'}"
    else:
        title = f"Group settings · {chat_name or 'this chat'}"
    mode = settings.get("trigger_mode") or ("trigger" if bot_name else "tag")
    mode_labels = {"trigger": "Name trigger", "tag": "Username trigger", "direct": "Direct"}
    bot_name = str(bot_name or "Hermes")
    username = str(bot_username or "").strip().lstrip("@")
    parts = [f"<h1>{_html_text(title, 128)}</h1>", "<p>Page 1 of 1</p>"]

    if not is_dm:
        subject = "channel post" if chat_type == "channel" else "group message"
        trigger_summary = {
            "trigger": f"A new {subject} must contain the bot name <b>{_html_text(bot_name, 32)}</b> or reply to one of this bot's messages.",
            "tag": (f"A new {subject} must contain <b>@{_html_text(username, 64)}</b> or reply to one of this bot's messages."
                    if username else f"A new {subject} must contain the bot username or reply to one of this bot's messages."),
            "direct": f"Every new {subject} is considered; access checks still apply. Enabling this requires a separate confirmation.",
        }
        if is_group:
            # The name/tag trigger is only an invocation trigger, never guest
            # authorization: only the owner and explicitly authorized users
            # can use the bot here.
            trigger_summary["trigger"] = (
                f"The bot name <b>{_html_text(bot_name, 32)}</b> or a reply to one of its messages "
                "is only an invocation trigger, not guest authorization. "
                "The owner and explicitly authorized users remain governed by their normal access."
            )
        parts.append(
            f"<h2>Invocation</h2><p><b>{_html_text(mode_labels.get(mode, 'Name trigger'), 32)}:</b> "
            f"{trigger_summary.get(mode, trigger_summary['trigger'])}</p>"
            "<p>Messages that do not meet the selected trigger are silently ignored. The invocation mode never grants access, changes global grants, or changes direct-message access.</p>"
        )
        trigger_word = str(settings.get("trigger_word") or "").strip()
        if trigger_word:
            parts.append(
                f"<p>Custom trigger word: <b>{_html_text(trigger_word, 64)}</b>. "
                "A message containing it also invokes the bot. Change it with <code>/trigger \"word\"</code>, or disable it with <code>/trigger \"off\"</code>.</p>"
            )
        if is_group:
            # The Triggers row introduces the settings-table shape to this
            # page: value is the word, or "off" when no word is set.
            parts.append(
                rich_html_table(
                    ("Setting", "Value", "Actions"),
                    [("Triggers", trigger_word or "off",
                      _settings_pill("hm:settings:triggers:open", "Open"))],
                )
            )
        if chat_type == "channel":
            parts.append(
                "<p>Channel access follows the existing gateway authorization rules.</p>"
            )
    if is_dm:
        parts.append("<p>Use the owner-only <code>/allowlist</code> command in the owner's private chat to review numeric account IDs and policy state.</p>")

    rows: list[list[tuple[str, str, str | None]]] = []
    if not is_dm:
        mode_buttons = []
        for key, label in (("trigger", "Name trigger"), ("tag", "Username"), ("direct", "Direct")):
            selected = mode == key
            text = label
            mode_buttons.append((text, f"hm:settings:mode:{key}", "success" if selected else None))
        rows.append(mode_buttons)
    if is_dm:
        rows.append([("Personal user settings", "hm:user:open", "primary")])
    rows.append([("GuestBot settings", "hm:help:guest:0", None), ("Help", "hm:help:menu:0", "primary")])
    if note:
        parts.append(f"<p>{_html_text(note)}</p>")
    return "\n".join(parts), inline_markup(rows)


ALLOWLIST_PAGE_SIZE = 20


def allowlist_view(users: list[dict[str, Any]], page_index: int) -> tuple[str, Any]:
    """Render one explicit 1-based page of global grants as native RichMessage HTML."""
    total = len(users)
    page_count = max(1, (total + ALLOWLIST_PAGE_SIZE - 1) // ALLOWLIST_PAGE_SIZE)
    if type(page_index) is not int or not 1 <= page_index <= page_count:
        raise ValueError("Allowlist page index is out of range")
    start = (page_index - 1) * ALLOWLIST_PAGE_SIZE
    page = users[start:start + ALLOWLIST_PAGE_SIZE]
    rows = [
        "<h1>Global allowlist</h1>",
        f"<p>Page {page_index} of {page_count} · {total} globally allowed account(s).</p>",
    ]
    if page:
        rows.append("<table><tr><th>Name</th><th>Username</th><th>Telegram ID</th><th>Status</th></tr>")
        for user in page:
            user_id = _html_text(user.get("user_id") or user.get("id") or "", 48)
            name = _html_text(user.get("name") or user_id, 64)
            username = str(user.get("username") or "").strip().lstrip("@")
            username_cell = "@" + _html_text(username, 64) if username else "—"
            status = "Banned; grant retained" if user.get("banned") is True else "Allowed"
            rows.append(
                "<tr><td>" + name + "</td><td>" + username_cell +
                "</td><td><code>" + user_id + "</code></td><td>" + status + "</td></tr>"
            )
        rows.append("</table>")
    else:
        rows.append("<p>No active global grants are recorded.</p>")
    rows.append("<p>Revoke one exact grant with <code>/disallow user_id</code>.</p>")
    controls = []
    if page_index > 1:
        controls.append(("Previous", f"hm:allowlist:page:{page_index - 1}", "primary"))
    if page_index < page_count:
        controls.append(("Next", f"hm:allowlist:page:{page_index + 1}", "primary"))
    markup = inline_markup([controls] if controls else [])
    return "\n".join(rows), markup


def allow_confirmation_view(
    user: dict[str, str], chat_label: str, token: str, *, isolated: bool = False,
) -> tuple[str, Any]:
    name = _plain(user.get("name") or user.get("id") or "Unknown user", 64)
    username = str(user.get("username") or "").strip().lstrip("@")
    user_id = _plain(user.get("id") or "", 24)
    lines = [
        "# Confirm access grant",
        "",
        f"You are about to allow **{name}**{f' · @{username}' if username else ''} (Telegram ID `{user_id}`) in **{_plain(chat_label, 48)}**.",
        ("This is a person-level grant across chats in this profile. They receive an isolated personal runtime, not the owner's profile or OS sudo. Confirm only if this is the intended account."
         if isolated else "They will receive the tool access configured for that scope. Confirm only if this is the intended account."),
    ]
    markup = inline_markup([[
        ("Allow this user", f"hm:allow:confirm:{token}", "success"),
        ("Cancel", f"hm:allow:cancel:{token}", "danger"),
    ]])
    return "\n".join(lines), markup
