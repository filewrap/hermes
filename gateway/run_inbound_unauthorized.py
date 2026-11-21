"""Copy and owner-side signalling for unauthorized inbound senders.

Two audiences, two rules:

* The **stranger** gets either a pairing code (behaviour ``pair``) or nothing at all (behaviour
  ``ignore``: an allowlist is configured, so any reply would leak that the bot exists).
* The **owner** is the one who can fix a mis-typed allowlist, so an ignored DM is logged at
  WARNING with the sender's ID and the allowlist / pairing-mode fix, and surfaced once per
  (platform, user) per gateway process in the platform's home channel when one is configured.
  No pairing request is minted for an ignored sender: strangers must not create server state
  when the owner has restricted access.
"""

from __future__ import annotations

import html
import logging
from collections import OrderedDict

from agent.i18n import t
from gateway.pairing import CODE_TTL_SECONDS, _allowlist_env_for_platform

# Display names come from the stranger. Bound them and keep the mention/markdown surface small in
# the owner's channel: a name is never a reason to render a link, mention or new section.
_MAX_SENDER_NAME_CHARS = 64
# Last-message excerpt in the Telegram home-channel drop notice: the owner reads it to judge the
# sender, but a stranger must not flood the log channel with a wall of text.
_MAX_DROP_MESSAGE_CHARS = 500
# Distinct (platform, user_id) senders remembered per process; a bot rotating IDs evicts the oldest
# instead of growing the set without limit.
_MAX_SEEN_SENDERS = 2048

logger = logging.getLogger("gateway.run")


def _safe_sender_name(user_name: str) -> str:
    """A stranger's display name, neutralized for the owner's channel: single line, no mention or
    link sigils, so a hostile name cannot ping the channel or smuggle a link."""
    from gateway.session import neutralize_untrusted_inline_text

    safe_name = neutralize_untrusted_inline_text(user_name or "", max_chars=_MAX_SENDER_NAME_CHARS)
    return "".join(ch for ch in safe_name if ch not in "@<>[]()`*_~#").strip()


def _telegram_drop_notice(*, user_id: str, user_name: str, message_text: str,
                          platform_name: str, hermes_home: str) -> str:
    """Rich home-channel notice for a dropped Telegram DM (Telegram only).

    Profile pill + user id + 'wants to talk' + the dropped message, then the
    short let-in guidance. The pill is the /start pattern (``rich_button``
    kind ``profile``): numeric id only, plain bold text otherwise. The
    message is HTML-escaped and capped so user content can neither break
    out of the quote nor flood the channel.
    """
    from plugins.platforms.telegram.mtproto_rich import rich_button

    safe_name = _safe_sender_name(user_name)
    label = safe_name or f"Customer {user_id}"
    if str(user_id).isdecimal():
        pill = rich_button(label, kind="profile", value=str(user_id))
    else:
        pill = f"<b>{html.escape(label)}</b>"
    text = str(message_text or "").strip()
    if not text:
        excerpt = "(no text)"
    else:
        if len(text) > _MAX_DROP_MESSAGE_CHARS:
            text = text[: _MAX_DROP_MESSAGE_CHARS - 1].rstrip() + "…"
        excerpt = html.escape(text)
    env_var = _allowlist_env_for_platform(platform_name)
    if env_var:
        allowlist = t("gateway.unauthorized.owner_hint_env",
                      env_var=f"<code>{html.escape(env_var)}</code>", home=hermes_home)
    else:
        allowlist = t("gateway.unauthorized.owner_hint_generic")
    guidance = t(
        "gateway.unauthorized.owner_hint_short",
        allowlist=allowlist,
        pair_setting="<code>unauthorized_dm_behavior: pair</code>",
        platform=platform_name, home=hermes_home,
    )
    wants_to_talk = t("gateway.unauthorized.wants_to_talk")
    return (
        f"<p>⚠️ {pill} ({html.escape(str(user_id))}) {wants_to_talk}:</p>"
        f"<blockquote>{excerpt}</blockquote>"
        f"<p>{guidance}</p>"
    )


def pairing_profile_arg(pairing_store) -> str:
    """``-p <profile> `` when the store belongs to a non-default profile, else ``""``."""
    store_profile = getattr(pairing_store, "profile", None)
    if isinstance(store_profile, str) and store_profile and store_profile != "default":
        return f"-p {store_profile} "
    return ""


def pairing_code_reply(platform_name: str, code: str, profile_arg: str = "") -> str:
    """The DM a first-time sender receives: what happened, how long the code lives, what to do
    whether they are the owner or a guest, and that they must message again after approval."""
    hours = max(1, CODE_TTL_SECONDS // 3600)
    validity = t("gateway.pairing.validity_hour" if hours == 1 else "gateway.pairing.validity_hours", hours=hours)
    approve_cmd = f"hermes {profile_arg}pairing approve {platform_name} {code}"
    return t("gateway.pairing.code_reply", code=code, validity=validity, approve_cmd=approve_cmd)


def pairing_rate_limited_reply() -> str:
    return t("gateway.pairing.rate_limited")


def unauthorized_owner_hint(
    platform_name: str, user_id: str, user_name: str = "", *, hermes_home: str,
) -> str:
    """One-line hint for the owner (log + home channel): who was dropped and how to let them in.
    No pairing request is minted for an ignored sender (a configured allowlist means the owner chose
    to restrict access), so the ways in are the allowlist itself or switching the platform to
    pairing mode."""
    safe_name = _safe_sender_name(user_name)
    # The ID next to the name is the value the owner actually acts on.
    who = f"{safe_name} ({user_id})" if safe_name else str(user_id)
    env_var = _allowlist_env_for_platform(platform_name)
    allowlist = (
        t("gateway.unauthorized.owner_hint_env", env_var=env_var, home=hermes_home)
        if env_var else t("gateway.unauthorized.owner_hint_generic")
    )
    return t("gateway.unauthorized.owner_notice", platform=platform_name, who=who, allowlist=allowlist, home=hermes_home)


class UnauthorizedOwnerNotifier:
    """Tells the owner's home channel about the first drop of each unrecognized DM sender.

    One notice per (platform, user_id) per gateway process: the first drop is the useful signal (an
    owner who typo'd their own ID); repeats would only let a stranger spam the home channel.
    """

    def __init__(self, max_seen: int = _MAX_SEEN_SENDERS) -> None:
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._max_seen = max(1, int(max_seen))

    def first_time(self, platform_name: str, user_id: str) -> bool:
        key = (platform_name, str(user_id))
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > self._max_seen:
            self._seen.popitem(last=False)
        return True

    async def notify(self, runner, source, hint: str, *, message_text: str = "",
                     hermes_home: str = "") -> None:
        """Best-effort post to the source platform's home channel; silent when none is configured.

        Telegram gets the rich drop notice (profile pill + id + 'wants to
        talk' + the dropped message + short let-in guidance); every other
        platform keeps the plain hint. The once-per-sender frequency is
        unchanged — ``first_time`` gates this call, never the message.
        """
        for platform, _cfg, home, transport in runner._home_channel_transports():
            if platform != source.platform:
                continue
            if str(home.chat_id) == str(source.chat_id):
                # The stranger's DM *is* the home channel (misconfiguration); posting there would
                # answer the unauthorized user, which the ignore behaviour exists to prevent.
                return
            # Automatic operator diagnostic on the home channel; same boundary as the state.db warning.
            from gateway.config import Platform
            from gateway.warning_notifications import present_notification

            if platform == Platform.TELEGRAM:
                notice = _telegram_drop_notice(
                    user_id=str(source.user_id),
                    user_name=str(source.user_name or ""),
                    message_text=message_text,
                    platform_name=platform.value,
                    hermes_home=hermes_home,
                )
            else:
                notice = f"⚠️ {hint}"
            await present_notification(
                lambda: runner._send_home_channel_message(
                    platform, home, transport, notice, "unauthorized-sender notice failed for %s:%s: %s",
                ),
                platform=platform,
            )
            return
