"""Privileged Telegram settings, per-chat grants, and button callbacks."""
from __future__ import annotations

import secrets
import re
import time
import logging
import json
from html import escape
from pathlib import Path
from typing import Any

from .telegram_chat_settings import (
    allow_confirmation_view,
    load_chat_settings,
    remove_allowed_user,
    settings_view,
    update_chat_settings,
    upsert_allowed_user,
)

logger = logging.getLogger(__name__)
_PENDING_ALLOW_SECONDS = 600


async def _notify_access_granted(adapter: Any, user: dict, *, direct: bool, chat_label: str) -> None:
    """Best effort only: a transport failure must never undo a saved grant.

    Do not retry ambiguous sends or log exception bodies (RPC errors may carry
    the whole request). The recipient has already started the bot in tenant mode.
    """
    from .access_ui import access_granted_view
    from .mtproto_rich import send_request
    from .start_ui import safe_start_error

    try:
        text, markup = access_granted_view(user, direct=direct, chat_label=chat_label)
        peer = await adapter._input_peer_for(str(user["id"]))
        from telethon import types
        # Never fall back to the callback's owner/group peer, even if a stale
        # cache or resolver returns a different entity for the recipient ID.
        if (not isinstance(peer, (types.InputPeerUser, types.InputPeerUserFromMessage))
                or str(peer.user_id) != str(user["id"])):
            raise ValueError("Access notice recipient is not the granted user's private peer")
        await adapter._mtproto_rich_request(send_request(peer, text, reply_markup=markup, no_webpage=True))
    except Exception as exc:
        logger.warning(
            "Telegram access notice delivery failed for user %s (%s); grant remains active. "
            "Check that the bot is reachable and not blocked; the user can send /help directly.",
            user["id"], safe_start_error(exc),
        )


async def _revoked_identity(adapter: Any, store: Any, user_id: str) -> dict:
    """Best-effort display metadata cannot block an otherwise valid revocation."""
    for method, args in (("resolve_started_user", (user_id,)), ("list_global_users", ())):
        reader = getattr(store, method, None)
        if not callable(reader):
            continue
        try:
            result = reader(*args)
            rows = result if isinstance(result, list) else [result]
            for row in rows:
                if not isinstance(row, dict) or str(row.get("user_id")) != user_id:
                    continue
                name = str(row.get("name") or "").strip()
                if name and name != user_id:
                    return {"id": user_id, "name": name}
        except Exception:
            logger.debug("Stored display name unavailable for revoked user %s", user_id)
    client = getattr(adapter, "_client", None)
    lookup = getattr(client, "get_entity", None)
    if callable(lookup):
        try:
            entity = await lookup(int(user_id))
            if str(getattr(entity, "id", "")) == user_id:
                name = " ".join(filter(None, [getattr(entity, "first_name", None),
                                             getattr(entity, "last_name", None)])).strip()
                if name:
                    return {"id": user_id, "name": name}
        except Exception:
            logger.debug("Telegram display name unavailable for revoked user %s", user_id)
    return {"id": user_id, "name": f"Telegram user {user_id}"}


def _home(adapter: Any, source: Any):
    runner = getattr(adapter, "gateway_runner", None)
    resolver = getattr(runner, "_resolve_profile_home_for_source", None)
    home = resolver(source) if callable(resolver) else None
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return home


def _scope(source: Any) -> str:
    if getattr(source, "chat_type", "") == "dm":
        return "dm"
    if getattr(source, "chat_type", "") in {"group", "forum", "channel"}:
        return "group"
    return ""


def _settings(adapter: Any, source: Any) -> dict[str, Any]:
    scope = _scope(source)
    reader = getattr(adapter, "_chat_settings", None)
    if callable(reader):
        return reader(source)
    return load_chat_settings(_home(adapter, source), scope, str(source.chat_id)) if scope else {}


def _tenant_store(adapter: Any, source: Any):
    resolver = getattr(adapter, "_multiuser_home", None)
    home = resolver(source) if callable(resolver) else None
    if home is not None:
        from gateway.mongo_multiuser import store_for_home
        return store_for_home(home)
    return None


def _chat_label(source: Any) -> str:
    if getattr(source, "chat_type", "") == "dm":
        return "all direct messages"
    return str(getattr(source, "chat_name", None) or source.chat_id)


def _allowlist_page(store: Any, home: Any, page_index: int, *, note: str = ""):
    """Build one allowlist page from durable global grants and bounded policy state."""
    users = store.list_global_users()
    from agent.tenant_policy import load_policy
    from .access_ui import ALLOWLIST_PAGE_SIZE, allowlist_view

    owner_id = str(store.owner_user_id)
    home_path = Path(home)
    ordered_ids = sorted(
        {str(user.get("user_id") or user.get("id")) for user in users
         if re.fullmatch(r"[1-9][0-9]*", str(user.get("user_id") or user.get("id") or ""))}
         | {owner_id},
        key=int,
    )
    offset = (page_index - 1) * ALLOWLIST_PAGE_SIZE
    user_policies = {
        uid: load_policy(home_path, uid)
        for uid in ordered_ids[offset:offset + ALLOWLIST_PAGE_SIZE]
        if uid != owner_id
    }
    return allowlist_view(
        users, page_index, default_policy=load_policy(home_path, "allowed"),
        user_policies=user_policies, owner_id=owner_id, note=note,
    )


def _allowlist_result_page(store: Any, home: Any, page_index: int, *, note: str = ""):
    try:
        return _allowlist_page(store, home, page_index, note=note)
    except ValueError:
        return _allowlist_page(store, home, 1, note=note)


def _settings_view(adapter: Any, source: Any, *, note: str = ""):
    if str(getattr(source, "chat_type", "")) == "dm":
        is_admin = bool(
            adapter._is_owner_user(getattr(source, "user_id", None), source=source)
            or adapter._is_explicitly_allowed(source)
        )
        if not is_admin:
            from .mtproto_rich import inline_markup
            return (
                "# Personal settings\n\nYour profile, memory, model, and learning preferences are private to your Telegram account.",
                inline_markup([[('Open personal settings', 'hm:user:open', 'primary')],
                               [('Help', 'hm:help:menu:0', None)]]),
            )
    return settings_view(
        str(getattr(source, "chat_type", "group")), _settings(adapter, source),
        chat_name=str(getattr(source, "chat_name", "") or ""),
        bot_name=str(getattr(adapter, "_bot_name", None) or "Hermes"),
        bot_username=str(getattr(adapter, "_bot_username", None) or ""),
        note=note,
        isolated=_tenant_store(adapter, source) is not None,
    )


async def show_settings(adapter: Any, source: Any, *, note: str = "", reply_to: int | None = None) -> None:
    text, markup = _settings_view(adapter, source, note=note)
    await adapter._send_rich_ui(source, text, markup, reply_to=reply_to)


def _trigger_view(adapter: Any, source: Any, *, note: str = "") -> str:
    """Rich Triggers card for one group: status line, explainer, disable hint.

    The status line carries the important word itself as a nested copy pill
    (the trigger word when enabled, ``off`` when disabled), so the word is
    tappable instead of prose. The bottom hint is the disable command as a
    copy pill while enabled, and the set command while disabled.
    """
    from .mtproto_rich import rich_button

    current = str(_settings(adapter, source).get("trigger_word") or "").strip()
    bot_name = str(getattr(adapter, "_bot_name", None) or "Hermes")
    parts = ["<h1>Triggers</h1>"]
    if note:
        parts.append(f"<p><i>{escape(note, quote=False)}</i></p>")
    parts.append(
        "<p>A trigger word invokes the bot in this group. "
        "A message containing it works like mentioning the bot by name.</p>"
    )
    parts.append("<hr>")
    parts.append("<h2>Invocation</h2>")
    if current:
        status = (
            "Current trigger is set to "
            f"{rich_button(current, kind='copy', value=current)}."
        )
    else:
        status = (
            "Current trigger is set to "
            f"{rich_button('off', kind='copy', value='off')}."
        )
    parts.append(f"<p>{status}</p>")
    parts.append(
        "<details><summary>What triggers are</summary>"
        f"<p>The bot name <b>{escape(bot_name, quote=False)}</b> or a reply to one of its messages "
        "is only an invocation trigger, not guest authorization. "
        "The owner and explicitly authorized users remain governed by their normal access.</p>"
        "<p>Messages that do not meet the selected trigger are silently ignored. "
        "The invocation mode never grants access, changes global grants, or changes direct-message access.</p>"
        "<p>A message containing the trigger word also invokes the bot.</p>"
        "</details>"
    )
    if current:
        disable_command = '/trigger "off"'
        hint = (
            "To disable it, send "
            f"{rich_button(disable_command, kind='copy', value=disable_command)}."
        )
    else:
        set_command = '/trigger "word"'
        hint = (
            "To set one, send "
            f"{rich_button(set_command, kind='copy', value=set_command)}."
        )
    parts.append(f"<p><i>{hint}</i></p>")
    return "\n".join(parts)


async def _answer(event: Any, text: str, *, alert: bool = False) -> None:
    try:
        await event.answer(text, alert=alert)
    except Exception:
        pass


async def _edit_or_send(adapter: Any, event: Any, source: Any, text: str, markup: Any = None,
                        *, edit_only: bool = False) -> None:
    if edit_only and markup is None:
        from telethon import types
        markup = types.ReplyInlineMarkup(rows=[])  # Remove the consumed confirmation keyboard.
    helper = getattr(adapter, "_edit_rich_ui_message", None)
    if callable(helper):
        await helper(event, source, text, markup)
        return
    from .mtproto_rich import edit_request
    try:
        await adapter._client(edit_request(
            adapter._peer_for(source.chat_id), int(getattr(event, "message_id", 0) or 0),
            text, reply_markup=markup,
        ))
    except Exception:
        if edit_only:
            raise
        await adapter._send_rich_ui(source, text, markup)


async def _callback_feedback(adapter: Any, event: Any, source: Any, text: str) -> None:
    """Show progress/result on the callback card instead of relying on a toast."""
    message_id = getattr(event, "message_id", None)
    if not message_id:
        return
    try:
        from .mtproto_rich import edit_request
        await adapter._mtproto_rich_request(edit_request(
            adapter._peer_for(source.chat_id), int(message_id), text,
        ))
    except Exception:
        logger.debug("Could not update Telegram access callback feedback", exc_info=True)


async def _apply_account_ban(adapter: Any, event: Any, source: Any, store: Any,
                             actor_id: str, target: str) -> bool:
    """Ban one account through the multiuser /ban path; return whether it applied.

    Shared by the pending-start admission card and the allowlist row button:
    the ban itself is the same store call either way.
    """
    from .multiuser import handle_command

    await _answer(event, "Applying ban…")
    await _callback_feedback(
        adapter, event, source,
        "<h2>Working…</h2><p>Applying the access ban. Please wait for the result below.</p>",
    )
    try:
        handled, response = handle_command(store, actor_id, f"/ban {target}")
    except Exception:
        await _callback_feedback(
            adapter, event, source,
            "<h2>Ban failed</h2><p>Could not ban that account. No successful change was confirmed.</p>",
        )
        return False
    readable = str(response or ("Account banned." if handled else "Ban was not applied."))
    await _callback_feedback(
        adapter, event, source,
        f"<h2>{'Ban complete' if handled else 'Ban not applied'}</h2><p>{escape(readable)}</p>",
    )
    return handled


async def resolve_replied_user_id(event: Any) -> str | None:
    """Return the sender of the message this command replies to, if any.

    Reuses Telethon's reply resolution (``message.get_reply_message``), the
    same mechanism the reaction and own-message-reply paths use. A missing or
    unresolvable reply is None, never an error.
    """
    message = getattr(event, "message", None)
    resolver = getattr(message, "get_reply_message", None)
    if not callable(resolver):
        return None
    try:
        reply = await resolver()
    except Exception:
        logger.debug("Telegram replied-to message unavailable", exc_info=True)
        return None
    sender_id = getattr(reply, "sender_id", None)
    if sender_id is None:
        return None
    text = str(sender_id).strip()
    return text if re.fullmatch(r"[1-9][0-9]*", text) else None


async def handle_owner_command(
    adapter: Any, source: Any, command: str, arguments: str, *, reply_to: int | None = None,
    confirmation_event: Any = None, reply_to_user_id: str | None = None,
) -> bool:
    """Handle a command that has already passed the Telegram explicit-access check.

    ``reply_to_user_id`` is the sender of the message the command replies to
    (resolved by the caller with :func:`resolve_replied_user_id`); /allow and
    /disallow accept it as the target when present.
    """
    async def respond(text: str, markup: Any = None):
        if confirmation_event is not None:
            await _edit_or_send(adapter, confirmation_event, source, text, markup, edit_only=True)
            return confirmation_event
        return await adapter._send_rich_ui(source, text, markup, reply_to=reply_to)

    command = str(command or "").lower()
    args = str(arguments or "").strip()
    if not (adapter._is_owner_user(source.user_id, source=source) or adapter._is_explicitly_allowed(source)):
        # Silent denial: non-admitted senders are ignored before they reach
        # this handler, and admitted users get no denial message here.
        return True
    is_owner = bool(adapter._is_owner_user(source.user_id, source=source))
    if command in {"allow", "allowlist", "disallow"} and not is_owner:
        # Owner-only command from an admitted non-owner: silent denial.
        return True
    home = _home(adapter, source)
    scope = _scope(source)
    if command == "settings":
        if args:
            await show_settings(adapter, source, note="Settings always apply to the chat where /settings is sent.", reply_to=reply_to)
        else:
            await show_settings(adapter, source, reply_to=reply_to)
        return True
    if command == "trigger":
        if source.chat_type not in {"group", "forum"}:
            await adapter._send_rich_ui(
                source, "`/trigger` works only in a group. Send it in the group whose trigger word you want to change.",
                reply_to=reply_to,
            )
            return True
        if not args:
            await adapter._send_rich_ui(source, _trigger_view(adapter, source), reply_to=reply_to)
            return True
        word = args.strip()
        if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
            word = word[1:-1].strip()
        if not word or re.search(r"\s", word):
            await adapter._send_rich_ui(
                source,
                "<h1>Trigger word</h1><p>Usage: <code>/trigger \"word\"</code> — one word only. "
                "Use <code>/trigger \"off\"</code> to disable the trigger word.</p>",
                reply_to=reply_to,
            )
            return True
        enabled_word = "" if word.lower() == "off" else word
        try:
            update_chat_settings(home, "group", str(source.chat_id), {"trigger_word": enabled_word})
        except Exception:
            await adapter._send_rich_ui(
                source,
                _trigger_view(adapter, source, note="Could not save this group setting."),
                reply_to=reply_to,
            )
            return True
        await adapter._send_rich_ui(source, _trigger_view(adapter, source), reply_to=reply_to)
        return True
    if command == "allowlist":
        if (source.chat_type not in {"dm", "private", "direct"}
                or str(source.chat_id) != str(source.user_id)):
            await adapter._send_rich_ui(source, "For privacy, use /allowlist in the configured owner's private chat with the bot.", reply_to=reply_to)
            return True
        store = _tenant_store(adapter, source)
        if store is None or not callable(getattr(store, "list_global_users", None)):
            await adapter._send_rich_ui(source, "The durable global allowlist is unavailable in this Telegram profile.", reply_to=reply_to)
            return True
        if args == "policy" or args.startswith("policy "):
            policy_args = args[len("policy"):].strip()
            subject = "allowed"
            payload = ""
            if policy_args:
                if policy_args.startswith("{"):
                    payload = policy_args
                else:
                    subject, _, payload = policy_args.partition(" ")
                    payload = payload.strip()
            if subject != "allowed" and not re.fullmatch(r"[1-9][0-9]*", subject):
                await adapter._send_rich_ui(source, "Usage: `/allowlist policy [allowed|numeric_id] [JSON object]`.", reply_to=reply_to)
                return True
            try:
                from agent.tenant_policy import load_policy, save_policy
                home_path = Path(_home(adapter, source))
                if payload:
                    policy = json.loads(payload)
                    if not isinstance(policy, dict):
                        raise ValueError("Policy JSON must be an object.")
                    save_policy(home_path, subject, policy)
                    saved = True
                else:
                    policy = load_policy(home_path, subject)
                    saved = False
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                message = str(exc) or "Invalid policy JSON."
                await adapter._send_rich_ui(source, escape(message), reply_to=reply_to)
                return True
            except Exception:
                logger.warning("Could not read or save Telegram access policy", exc_info=True)
                await adapter._send_rich_ui(source, "Could not read or save that access policy. No unsupported policy was applied.", reply_to=reply_to)
                return True
            from .access_ui import policy_view
            await adapter._send_rich_ui(source, policy_view(subject, policy, saved=saved), reply_to=reply_to)
            return True
        if args and not re.fullmatch(r"[1-9][0-9]*", args):
            await adapter._send_rich_ui(source, "Usage: `/allowlist [page_index]` or `/allowlist policy [allowed|numeric_id] [JSON object]`.", reply_to=reply_to)
            return True
        page_index = int(args) if args else 1
        try:
            text, markup = _allowlist_page(store, _home(adapter, source), page_index)
        except ValueError:
            users = store.list_global_users()
            from .access_ui import ALLOWLIST_PAGE_SIZE
            page_count = max(1, (len(users) + 1 + ALLOWLIST_PAGE_SIZE - 1) // ALLOWLIST_PAGE_SIZE)
            await adapter._send_rich_ui(source, f"Page index is out of range. Choose a page from 1 to {page_count}.", reply_to=reply_to)
            return True
        await adapter._send_rich_ui(source, text, markup, reply_to=reply_to)
        return True
    if command == "disallow":
        usage = "Usage: `/disallow <numeric user id>`, `/disallow @username`, or reply to the user's message with `/disallow`."
        user_id = reply_to_user_id
        if user_id is None:
            fields = args.split()
            if len(fields) != 1:
                await adapter._send_rich_ui(source, usage, reply_to=reply_to)
                return True
            raw = fields[0]
            if re.fullmatch(r"[1-9][0-9]*", raw):
                user_id = raw
            else:
                username = raw.lstrip("@")
                lookup = getattr(getattr(adapter, "_client", None), "get_entity", None)
                if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,63}", username)
                        or not callable(lookup)):
                    await adapter._send_rich_ui(source, usage, reply_to=reply_to)
                    return True
                try:
                    entity = await lookup("@" + username)
                except Exception:
                    logger.debug("Telegram username lookup failed for /disallow", exc_info=True)
                    entity = None
                if entity is None or not re.fullmatch(r"[1-9][0-9]*", str(getattr(entity, "id", "") or "")):
                    await adapter._send_rich_ui(
                        source,
                        "No access revoked. I could not resolve that @username to a Telegram user. Use their numeric user ID, or reply to one of their messages.",
                        reply_to=reply_to,
                    )
                    return True
                user_id = str(entity.id)
        store = _tenant_store(adapter, source)
        if store is None or not callable(getattr(store, "allow_user", None)):
            await adapter._send_rich_ui(source, "The durable global allowlist is unavailable in this Telegram profile.", reply_to=reply_to)
            return True
        if user_id == str(getattr(store, "owner_user_id", "")):
            await adapter._send_rich_ui(source, "The configured owner grant cannot be revoked.", reply_to=reply_to)
            return True
        try:
            if not store.get_access(user_id).get("allowed"):
                await adapter._send_rich_ui(source, f"Telegram ID {user_id} has no global allowed grant to revoke.", reply_to=reply_to)
                return True
            user = await _revoked_identity(adapter, store, user_id)
            from agent.mongo_tenants import Principal
            store.allow_user(Principal(str(source.user_id)), user_id, False)
        except Exception:
            await adapter._send_rich_ui(source, "Could not revoke that global access grant.", reply_to=reply_to)
            return True
        from .access_ui import disallow_success_view
        await adapter._send_rich_ui(
            source, disallow_success_view(user),
            reply_to=reply_to,
        )
        return True
    if command == "allow":
        if not scope or source.chat_type not in {"dm", "private", "direct", "group", "forum"}:
            await adapter._send_rich_ui(source, "Access grants are available in a group or direct message.", reply_to=reply_to)
            return True
        # The replied-to user wins over an argument: replying to a message
        # names its sender unambiguously.
        target = reply_to_user_id or ""
        if not target:
            if not args or len(args.split()) != 1:
                await adapter._send_rich_ui(
                    source, "# Allow an account\n\nUsage: `/allow @username`, `/allow user_id`, or reply to the user's message with `/allow`.\nIn tenant mode this grants the account access across chats in this profile, using its own isolated runtime.",
                    reply_to=reply_to,
                )
                return True
            target = args.split(maxsplit=1)[0]
        store = _tenant_store(adapter, source)
        if store is not None:
            # A Telegram username lookup alone is not proof that the user has
            # started this bot. Resolve only canonical, authenticated /start records.
            identity = target
            if not target.isdecimal():
                try:
                    entity = await adapter._client.get_entity("@" + target.lstrip("@"))
                    if bool(getattr(entity, "bot", False)) or not hasattr(entity, "first_name"):
                        raise ValueError("Not a human Telegram user")
                    identity = str(entity.id)
                except Exception:
                    await respond("No access granted. I could not verify that current @username. Ask the user to /start this bot, or use their numeric user ID.")
                    return True
            user = store.resolve_started_user(identity)
            if not user:
                await respond("No access granted. That user must first send `/start` to this bot in a private chat. Then retry with their current @username or numeric user ID.")
                return True
            user_id = str(user["user_id"])
            access = store.get_access(user_id)
            if access.get("banned"):
                await respond("No access granted. This account is banned; only the owner can unban it first.")
                return True
            user = {"id": user_id, "name": user.get("name") or user_id, "username": user.get("username") or ""}
            entity = None
        else:
            lookup = target if target.startswith("@") or not target.isdigit() else int(target)
            try:
                entity = await adapter._client.get_entity(lookup)
            except Exception:
                entity = None
        user_id = str(user["id"]) if store is not None else str(getattr(entity, "id", "") or "").strip()
        is_user = store is not None or entity is not None and (hasattr(entity, "first_name") or hasattr(entity, "bot"))
        if store is None and (not user_id or not is_user):
            await show_settings(
                adapter, source,
                note="I could not verify that Telegram user, so no access was granted. Use their exact public @username or ask them to contact the bot first, then use their numeric ID.",
                reply_to=reply_to,
            )
            return True
        if store is None and (bool(getattr(entity, "bot", False)) or user_id == str(getattr(adapter, "_bot_id", ""))):
            await show_settings(adapter, source, note="Bot accounts cannot be added through /allow.", reply_to=reply_to)
            return True
        if store is None:
            name = " ".join(filter(None, [getattr(entity, "first_name", None), getattr(entity, "last_name", None)])).strip()
            username = str(getattr(entity, "username", "") or "").strip().lstrip("@")
            user = {"id": user_id, "name": name or username or f"Telegram user {user_id}", "username": username}
        token = secrets.token_urlsafe(6)
        pending = getattr(adapter, "_pending_allow_requests", None)
        if not isinstance(pending, dict):
            pending = {}
            adapter._pending_allow_requests = pending
        pending[token] = {
            "created": time.monotonic(), "owner_id": str(source.user_id or ""),
            "chat_id": str(source.chat_id), "scope": "all" if store is not None else scope, "user": user,
            "home": str(Path(home).resolve()), "chat_scope": scope,
            "policy_home": str(Path(store.home).resolve()) if store is not None else None,
        }
        label = "all chats in this profile" if store is not None else _chat_label(source)
        text, markup = allow_confirmation_view(user, label, token, isolated=store is not None)
        try:
            receipt = await respond(text, markup)
            message_id = (getattr(confirmation_event, "message_id", None)
                          if confirmation_event is not None else getattr(receipt, "id", None))
            if not str(message_id or "").isdecimal():
                raise ValueError("The access confirmation message could not be bound")
            pending[token]["message_id"] = str(message_id)
            # A re-rendered card must not retain an invisible older capability.
            for old_token, old in list(pending.items()):
                if old_token != token and all(old.get(key) == pending[token].get(key) for key in (
                    "home", "policy_home", "owner_id", "chat_id", "message_id",
                )):
                    pending.pop(old_token, None)
        except Exception:
            pending.pop(token, None)
            raise
        return True
    return False


async def handle_owner_callback(adapter: Any, event: Any, source: Any, data: str) -> bool:
    """Apply privileged settings and the two-step /allow grant."""
    parts = str(data or "").split(":")
    if len(parts) < 3 or parts[0] != "hm":
        return False
    if parts[1] not in {"settings", "allow", "allowlist", "admission"}:
        return False
    if not (adapter._is_owner_user(getattr(source, "user_id", None), source=source) or adapter._is_explicitly_allowed(source)):
        await _answer(event, "This account cannot change these settings.", alert=True)
        return True
    if parts[1] == "settings" and len(parts) == 5 and parts[2] == "direct":
        actor_id = str(getattr(source, "user_id", "") or "")
        if not adapter._is_owner_user(actor_id, source=source):
            await _answer(event, "Only the configured owner can enable Direct mode.", alert=True)
            return True
        token, decision = parts[3], parts[4]
        pending = getattr(adapter, "_pending_direct_mode", {})
        request = pending.get(token) if isinstance(pending, dict) else None
        home = str(Path(_home(adapter, source)).resolve())
        valid = (
            isinstance(request, dict)
            and time.monotonic() - float(request.get("created", 0)) <= 300
            and request.get("owner_id") == actor_id
            and request.get("chat_id") == str(source.chat_id)
            and request.get("home") == home
            and request.get("message_id") == str(getattr(event, "message_id", ""))
            and request.get("thread_id") == str(getattr(source, "thread_id", "") or "")
            and source.chat_type in {"group", "forum", "channel"}
            and decision in {"confirm", "cancel"}
        )
        if not valid:
            await _answer(event, "This Direct-mode confirmation expired or belongs to another chat.", alert=True)
            return True
        pending.pop(token, None)
        if decision == "cancel":
            note = "Direct mode was not enabled. No setting was changed."
        else:
            try:
                update_chat_settings(home, "group", str(source.chat_id), {"trigger_mode": "direct"})
            except Exception:
                await _answer(event, "Could not save Direct mode. No setting was changed.", alert=True)
                return True
            note = "Direct mode is now enabled for this chat."
        await _answer(event, note)
        text, markup = _settings_view(adapter, source, note=note)
        await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
        return True
    if parts[1] == "admission":
        # Never treat visible/forwarded callback data as authority. Even an
        # explicitly allowed admin cannot use the owner's private admission UI.
        store = _tenant_store(adapter, source)
        actor_id = str(getattr(source, "user_id", "") or "")
        if (not adapter._is_owner_user(actor_id, source=source)
                or source.chat_type not in {"dm", "private", "direct"}
                or str(source.chat_id) != actor_id
                or store is None or str(store.owner_user_id) != actor_id):
            await _answer(event, "This control is only available in the owner's private chat.", alert=True)
            return True
        if (len(parts) != 4 or parts[2] not in {"allow", "ban"}
                or not re.fullmatch(r"[1-9][0-9]*", parts[3])):
            await _answer(event, "This access action is unavailable.", alert=True)
            return True
        target = parts[3]
        if parts[2] == "allow":
            # Open the existing two-step owner grant; viewing or clicking this
            # notification does not bypass eligibility/confirmation checks.
            await _answer(event, "Preparing access confirmation…")
            try:
                return await handle_owner_command(
                    adapter, source, "allow", target, confirmation_event=event,
                )
            except Exception:
                logger.warning("Could not open Telegram access confirmation", exc_info=True)
                await _callback_feedback(
                    adapter, event, source,
                    "<h2>Access confirmation failed</h2><p>No access was granted. Try the owner-only /allow command again.</p>",
                )
                return True
        await _apply_account_ban(adapter, event, source, store, actor_id, target)
        return True
    if parts[1] == "allowlist":
        actor_id = str(getattr(source, "user_id", "") or "")
        if (not adapter._is_owner_user(actor_id, source=source)
                or source.chat_type not in {"dm", "private", "direct"}
                or str(source.chat_id) != actor_id):
            await _answer(event, "Only the configured owner can manage the allowlist in their private chat.", alert=True)
            return True
        if len(parts) not in {4, 5}:
            await _answer(event, "This allowlist page is unavailable.", alert=True)
            return True
        store = _tenant_store(adapter, source)
        if store is None or not callable(getattr(store, "list_global_users", None)):
            await _answer(event, "The durable global allowlist is unavailable.", alert=True)
            return True
        if str(store.owner_user_id) != actor_id:
            await _answer(event, "This allowlist does not belong to the configured owner.", alert=True)
            return True
        home = str(Path(_home(adapter, source)).resolve())
        action, subject = parts[2], parts[3]
        if len(parts) == 4 and action == "page" and re.fullmatch(r"[1-9][0-9]*", subject):
            page_index = int(subject)
            try:
                text, markup = _allowlist_page(store, home, page_index)
            except ValueError:
                await _answer(event, "That allowlist page no longer exists.", alert=True)
                return True
            await _answer(event, "")
            await _edit_or_send(adapter, event, source, text, markup)
            return True

        if (action == "revoke" and len(parts) == 5
                and re.fullmatch(r"[1-9][0-9]*", subject)
                and re.fullmatch(r"[1-9][0-9]*", parts[4])):
            user_id = subject
            page_index = int(parts[4])
            if user_id == actor_id:
                await _answer(event, "The configured owner's access cannot be revoked.", alert=True)
                return True
            if not str(getattr(event, "message_id", "") or "").isdecimal():
                await _answer(event, "This allowlist card cannot be bound to a confirmation.", alert=True)
                return True
            try:
                if not store.get_access(user_id).get("allowed"):
                    await _answer(event, "That account has no global grant to revoke.", alert=True)
                    return True
                user = await _revoked_identity(adapter, store, user_id)
            except Exception:
                await _answer(event, "Could not verify that global grant.", alert=True)
                return True
            token = secrets.token_urlsafe(6)
            pending = getattr(adapter, "_pending_disallow_requests", None)
            if not isinstance(pending, dict):
                pending = {}
                adapter._pending_disallow_requests = pending
            message_id = str(getattr(event, "message_id", "") or "")
            pending[token] = {
                "created": time.monotonic(), "owner_id": actor_id,
                "chat_id": str(source.chat_id), "home": home,
                "message_id": message_id, "user_id": user_id,
                "page_index": page_index,
            }
            from .mtproto_rich import inline_markup
            text = (
                "<h2>Confirm global access revocation</h2>"
                f"<p>Revoke the global grant for <b>{escape(user.get('name') or user_id)}</b> "
                f"(<code>{escape(user_id)}</code>)?</p>"
                "<p>This removes only the global grant. It does not ban the account or change group guest access.</p>"
            )
            markup = inline_markup([[
                ("Confirm revoke", f"hm:allowlist:confirm:{token}", "danger"),
                ("Cancel", f"hm:allowlist:cancel:{token}", "primary"),
            ]])
            try:
                await _answer(event, "Review the revocation before confirming.")
                await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
            except Exception:
                pending.pop(token, None)
                raise
            return True

        if (action == "ban" and len(parts) == 5
                and re.fullmatch(r"[1-9][0-9]*", subject)
                and re.fullmatch(r"[1-9][0-9]*", parts[4])):
            user_id = subject
            page_index = int(parts[4])
            if user_id == actor_id:
                await _answer(event, "The configured owner's access cannot be banned.", alert=True)
                return True
            handled = await _apply_account_ban(adapter, event, source, store, actor_id, user_id)
            note = (f"Banned Telegram ID {user_id}."
                    if handled else f"Ban was not applied to Telegram ID {user_id}.")
            text, markup = _allowlist_result_page(store, home, page_index, note=note)
            await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
            return True

        if len(parts) == 4 and action in {"confirm", "cancel"}:
            pending = getattr(adapter, "_pending_disallow_requests", {})
            request = pending.get(subject) if isinstance(pending, dict) else None
            valid = (
                isinstance(request, dict)
                and time.monotonic() - float(request.get("created", 0)) <= _PENDING_ALLOW_SECONDS
                and str(request.get("owner_id")) == actor_id
                and str(request.get("chat_id")) == str(source.chat_id)
                and request.get("home") == home
                and request.get("message_id") == str(getattr(event, "message_id", None))
                and request.get("user_id") != actor_id
                and re.fullmatch(r"[1-9][0-9]*", str(request.get("user_id", "")))
            )
            if not valid:
                await _answer(event, "This revocation confirmation expired or belongs to another chat.", alert=True)
                return True
            pending.pop(subject, None)
            page_index = int(request.get("page_index", 1))
            if action == "cancel":
                await _answer(event, "Cancelled. No access changes were made.")
                text, markup = _allowlist_result_page(
                    store, home, page_index, note="Revocation cancelled; no access changes were made.",
                )
                await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
                return True
            user_id = str(request["user_id"])
            try:
                if not store.get_access(user_id).get("allowed"):
                    raise ValueError("The global grant is no longer active")
                from agent.mongo_tenants import Principal
                store.allow_user(Principal(actor_id), user_id, False)
            except Exception:
                await _answer(event, "Could not revoke that global grant; no ban was applied.", alert=True)
                text, markup = _allowlist_result_page(
                    store, home, page_index, note="Revocation failed. No ban was applied.",
                )
                await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
                return True
            await _answer(event, "Global access revoked. The account was not banned.")
            text, markup = _allowlist_result_page(
                store, home, page_index,
                note=f"Revoked global access for Telegram ID {user_id}. No ban was applied.",
            )
            await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
            return True

        await _answer(event, "This allowlist action is unavailable.", alert=True)
        return True
    scope = _scope(source)
    if parts[1] == "allow":
        if not adapter._is_owner_user(getattr(source, "user_id", None), source=source):
            await _answer(event, "Only the configured owner can grant person-level access.", alert=True)
            return True
        if len(parts) != 4 or parts[2] not in {"confirm", "cancel"}:
            await _answer(event, "This access action is unavailable.", alert=True)
            return True
        token = parts[3]
        pending = getattr(adapter, "_pending_allow_requests", {})
        request = pending.get(token) if isinstance(pending, dict) else None
        store = _tenant_store(adapter, source)
        valid = (
            isinstance(request, dict)
            and time.monotonic() - float(request.get("created", 0)) <= _PENDING_ALLOW_SECONDS
            and str(request.get("owner_id")) == str(source.user_id or "")
            and str(request.get("chat_id")) == str(source.chat_id)
            and request.get("home") == str(Path(_home(adapter, source)).resolve())
            and request.get("policy_home") == (str(Path(store.home).resolve()) if store is not None else None)
            and request.get("chat_scope") == scope
            and request.get("scope") == ("all" if store is not None else scope)
            and request.get("message_id") == str(getattr(event, "message_id", None))
        )
        if not valid:
            await _answer(event, "This access confirmation expired or belongs to another chat.", alert=True)
            return True
        pending.pop(token, None)
        if parts[2] == "cancel":
            await _answer(event, "Cancelled. No access was granted.")
            await _edit_or_send(adapter, event, source,
                                "# Access grant cancelled\n\nNo changes were made.", edit_only=True)
            return True
        try:
            user = request["user"]
            if store is not None:
                from agent.mongo_tenants import Principal
                access = store.get_access(user["id"])
                if access.get("banned") or not store.resolve_started_user(user["id"]):
                    raise ValueError("Recipient is no longer eligible for this grant")
                newly_added = not access.get("allowed")
                store.allow_user(Principal(str(source.user_id)), user["id"], True)
            else:
                newly_added = user["id"] not in (_settings(adapter, source).get("allowed_users") or {})
                upsert_allowed_user(_home(adapter, source), scope, str(source.chat_id), user)
        except Exception:
            await _answer(event, "Could not grant access. The user must have started this bot, must not be banned, and you must still be authorized in this scope.", alert=True)
            await _edit_or_send(
                adapter, event, source,
                "# Access not granted\n\nThe account is no longer eligible or the grant could not be saved. No access was granted by this confirmation. Start a new owner-only /allow request to retry.",
                edit_only=True,
            )
            return True
        if newly_added:
            await _notify_access_granted(
                adapter, user, direct=store is not None or scope == "dm", chat_label=_chat_label(source),
            )
        from .access_ui import allow_success_view
        await _answer(event, f"Allowed {user.get('name') or user.get('id')}.")
        await _edit_or_send(
            adapter, event, source,
            allow_success_view(user, isolated=store is not None, chat_label=_chat_label(source)),
            edit_only=True,
        )
        return True
    if len(parts) != 4:
        await _answer(event, "This settings action is unavailable.", alert=True)
        return True
    action, value = parts[2], parts[3]
    if action == "triggers" and source.chat_type in {"group", "forum"}:
        from .mtproto_rich import inline_markup

        if value == "open":
            await _answer(event, "")
            await _edit_or_send(
                adapter, event, source, _trigger_view(adapter, source),
                inline_markup([[("Back", "hm:settings:triggers:back", "primary")]]),
            )
            return True
        if value == "back":
            await _answer(event, "")
            text, markup = _settings_view(adapter, source)
            await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
            return True
    try:
        if action == "mode" and scope == "group" and value in {"trigger", "tag", "direct"}:
            if value == "direct":
                actor_id = str(getattr(source, "user_id", "") or "")
                if not adapter._is_owner_user(actor_id, source=source):
                    await _answer(event, "Only the configured owner can enable Direct mode.", alert=True)
                    return True
                message_id = str(getattr(event, "message_id", "") or "")
                if not message_id.isdecimal():
                    await _answer(event, "This settings card cannot be bound to a confirmation.", alert=True)
                    return True
                pending = getattr(adapter, "_pending_direct_mode", None)
                if not isinstance(pending, dict):
                    pending = adapter._pending_direct_mode = {}
                now = time.monotonic()
                for old_token, old_request in list(pending.items()):
                    if not isinstance(old_request, dict) or now - float(old_request.get("created", 0)) > 300:
                        pending.pop(old_token, None)
                while len(pending) >= 100:
                    pending.pop(next(iter(pending)))
                token = secrets.token_urlsafe(6)
                pending[token] = {
                    "created": now, "owner_id": actor_id, "chat_id": str(source.chat_id),
                    "home": str(Path(_home(adapter, source)).resolve()),
                    "message_id": message_id,
                    "thread_id": str(getattr(source, "thread_id", "") or ""),
                }
                from .mtproto_rich import inline_markup
                text = (
                    "<h2>Confirm Direct mode</h2>"
                    "<p>Direct mode sends every new message in this chat to Hermes, subject to the existing access and safety checks.</p>"
                    "<p>This is broader than Name or Username triggers. Confirm only if you intend that behavior.</p>"
                )
                markup = inline_markup([[
                    ("Confirm Direct", f"hm:settings:direct:{token}:confirm", "danger"),
                    ("Cancel", f"hm:settings:direct:{token}:cancel", "primary"),
                ]])
                await _answer(event, "Review the Direct-mode change before confirming.")
                await _edit_or_send(adapter, event, source, text, markup, edit_only=True)
                return True
            update_chat_settings(_home(adapter, source), "group", str(source.chat_id), {"trigger_mode": value})
            note = f"Group invocation mode is now {value}."
        elif action == "remove" and scope:
            store = _tenant_store(adapter, source)
            if store is not None:
                actor = store.principal_for_scope(str(source.user_id), source.chat_type, str(source.chat_id))
                store.revoke_sudo(actor, value, scope="dm" if scope == "dm" else str(source.chat_id))
            else:
                remove_allowed_user(_home(adapter, source), scope, str(source.chat_id), value)
            note = f"Removed Telegram ID {value} from this scope's explicit allowlist."
        else:
            await _answer(event, "This settings action is unavailable in this chat.", alert=True)
            return True
    except Exception:
        await _answer(event, "Could not save this setting.", alert=True)
        return True
    await _answer(event, note)
    text, markup = _settings_view(adapter, source, note=note)
    await _edit_or_send(adapter, event, source, text, markup)
    return True
