"""Telegram Business Mode (chat automation) — direct replies.

Telethon-native port of the upstream ``telegram-business`` plugin's
``manager.py``. When a Telegram Business account owner connects this bot
via BotFather's Business Mode, the bot starts receiving messages
addressed to the owner in chats the owner whitelisted. This module turns
those incoming customer messages into replies sent DIRECTLY to the
customer via the business connection — no draft, no approval, no guard.
The owner still sees what was sent (Recent + a minimal sent notice).

TL shapes (proven against layer-233 ``api.tl`` + installed Telethon —
Bot API / PTB shapes do NOT apply here):

  * ``BotBusinessConnection`` fields: ``connection_id, user_id, dc_id,
    date, disabled, rights``. There is NO ``user_chat_id`` — the owner DM
    chat id IS ``user_id``. ``is_enabled = not disabled``.
  * ``BusinessBotRights`` has NO ``can_reply`` field. The reply right is
    ``rights.reply``. No top-level ``can_reply`` fallback exists on TL
    objects, so none is read.
  * ``Message`` has NO ``business_connection_id`` field — the connection
    id comes from the ``updateBotNewBusinessMessage.connection_id`` field,
    so ``handle_business_message`` takes ``(connection_id, message)``.
    ``from_id`` is a Peer (``telethon.utils.get_peer_id`` resolves it);
    ``via_business_bot_id`` on the Message marks a bot-sent message.
  * ``UpdateBotDeleteBusinessMessage`` fields: ``connection_id, peer,
    messages, qts`` — ``handle_business_delete`` takes
    ``(connection_id, peer, message_ids)``.

The manager never imports the adapter: every Telegram I/O path is an
injected callable (constructor). Customer delivery goes ONLY through the
injected ``send_business_message`` callable — the manager never sends to
a customer any other way. Owner DMs go through ``send_message``.

Replying is an agent turn supplied by the adapter as ``reply_via_turn``:
``reply_via_turn(customer_text, customer_chat_id, context) -> str``
(async, raises on failure). ``context`` carries the connection id, the
customer name, the matched trigger (``direct`` / ``word`` / ``reply`` /
``owner``), the last 8 recent messages for that customer chat, and the
owner persona. The turn's final text IS the reply — the adapter sends
it directly. ``set_trigger`` / ``set_persona`` are in-memory only — the
manager does NOT write ``config.yaml``; the config worker persists what
they return.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon.utils import get_peer_id

logger = logging.getLogger(__name__)


# Callback-data prefix for the inline buttons. Keep it short — Telegram
# caps callback_data at 64 bytes. Format: "hm:biz:<action>" for card
# buttons, "hm:biz:<action>:<customer_chat_id>" for per-chat buttons.
CALLBACK_PREFIX = "hm:biz:"

# Card-button actions. The manager returns the action name and lets the
# UI worker own rendering; it never renders or mutates for these — except
# mute/unmute (per-chat, applied here) and trigger_word/persona (which
# arm the owner-input capture below).
CARD_ACTIONS = {"pause", "resume", "trigger", "trigger_word", "persona",
                "mute", "unmute", "muted", "recent", "status", "connections"}

TRIGGER_MODES = {"direct", "trigger"}

# BusinessBotRights flag names (layer-233 api.tl businessBotRights).
RIGHTS_FIELDS = (
    "reply", "read_messages", "delete_sent_messages",
    "delete_received_messages", "edit_name", "edit_bio",
    "edit_profile_photo", "edit_username", "view_gifts", "sell_gifts",
    "change_gift_settings", "transfer_and_upgrade_gifts",
    "transfer_stars", "manage_stories",
)

DEFAULT_PERSONA = (
    "You are replying on behalf of the account owner. "
    "Match the tone of a personal message — warm, direct, and concise. "
    "Do not introduce yourself as an assistant or AI. "
    "Reply in the same language the customer used."
)


# Onboarding text the bot DMs the owner the first time a BusinessConnection
# arrives. Rich HTML (first line a blockquote, no emojis, commands as plain
# text, one italic bottom line) sent through the adapter's rich-UI path.
ONBOARDING_MESSAGE = (
    "<blockquote>You've connected me as your Telegram Business assistant.</blockquote>"
    "<p>When someone messages you, I reply to them directly, as you.</p>"
    "<p>• /biz — status and controls</p>"
    "<p>• /biz pause — stop replying for a while</p>"
    "<i>I answer as you.</i>"
)


# ---------------------------------------------------------------------------
# Type aliases for the adapter callbacks the manager depends on.
# Defined as Callables so this module stays decoupled from the adapter.
# ---------------------------------------------------------------------------

# Generates a reply text for a customer message via an agent turn.
# Returns the reply text, or raises on failure.
ReplyViaTurn = Callable[[str, str, Dict[str, Any]], Awaitable[str]]

# Sends a message to a chat (owner DMs only, from this module's side).
SendMessage = Callable[..., Awaitable[Any]]

# Delivers a message to a customer through the business connection
# (the adapter's InvokeWithBusinessConnection wrapper). The ONLY path
# this module uses to reach a customer.
SendBusinessMessage = Callable[..., Awaitable[Any]]


def business_triggered(
    text: Optional[str], *, mode: str, word: str, reply_to_business_bot: bool,
) -> bool:
    """Pure trigger gate (design §4).

    ``direct``: any non-empty text triggers. ``trigger``: triggers when
    the text contains the trigger word (word-boundary, case-insensitive —
    the same rule as the group word trigger) or when the message replies
    to a message the business bot sent. Any other mode never triggers.
    The trigger never grants access: the sender is always the customer by
    construction of the intake guard.
    """
    text = (text or "").strip()
    if not text:
        return False
    if mode == "direct":
        return True
    if mode == "trigger":
        if reply_to_business_bot:
            return True
        needle = (word or "").strip()
        if not needle:
            return False
        return re.search(
            rf"(?<!\w){re.escape(needle)}(?!\w)", text, re.IGNORECASE,
        ) is not None
    return False


def _peer_id_str(peer: Any) -> Optional[str]:
    """A TL Peer (or duck-typed peer) as its plain id string."""
    if peer is None:
        return None
    try:
        return str(get_peer_id(peer))
    except Exception:
        logger.debug("get_peer_id failed for business peer %r", peer, exc_info=True)
    for attr in ("user_id", "chat_id", "channel_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            return str(val)
    return str(peer)


def _sender_id(message: Any) -> Optional[str]:
    """The message sender's id, or None when it cannot be determined."""
    from_id = getattr(message, "from_id", None)
    if from_id is not None:
        return _peer_id_str(from_id)
    user = getattr(message, "from_user", None)
    if user is not None:
        sender = getattr(user, "id", None)
        return str(sender) if sender is not None else None
    return None


def _customer_chat_id(message: Any) -> Optional[str]:
    peer = getattr(message, "peer_id", None)
    if peer is not None:
        return _peer_id_str(peer)
    chat = getattr(message, "chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return str(chat.id)
    return None


def _customer_msg_id(message: Any) -> Optional[str]:
    msg_id = getattr(message, "id", None)
    if msg_id is None:
        msg_id = getattr(message, "message_id", None)
    return str(msg_id) if msg_id is not None else None


def _message_text(message: Any) -> str:
    """Raw text of a TL Message (``message`` holds text and captions)."""
    for attr in ("message", "text", "caption"):
        val = getattr(message, attr, None)
        if val:
            return str(val)
    return ""


def _is_group_chat(message: Any) -> bool:
    """True when the business chat is a group/channel, not a private chat."""
    peer = getattr(message, "peer_id", None)
    if peer is not None:
        if type(peer).__name__ in {"PeerChat", "PeerChannel"}:
            return True
        if getattr(peer, "chat_id", None) is not None:
            return True
        if getattr(peer, "channel_id", None) is not None:
            return True
        return False
    chat = getattr(message, "chat", None)
    if chat is not None:
        return getattr(chat, "type", "private") != "private"
    return False


def _customer_display_name(message: Any) -> str:
    """Best-effort human name for the customer who sent ``message``."""
    try:
        sender = getattr(message, "sender", None)
    except Exception:
        logger.debug("Business sender lookup failed", exc_info=True)
        sender = None
    for obj in (sender, getattr(message, "from_user", None), getattr(message, "chat", None)):
        if obj is None:
            continue
        for attr in ("full_name", "first_name", "username", "title"):
            val = getattr(obj, attr, None)
            if val:
                return str(val)
    return "Customer"


def _rights_dict(rights: Any) -> Dict[str, bool]:
    """A TL ``BusinessBotRights`` as a plain {flag: bool} dict."""
    if rights is None:
        return {}
    return {name: bool(getattr(rights, name, False)) for name in RIGHTS_FIELDS}


def _customer_pill(name: str, chat_id: str) -> str:
    """Customer name as a profile pill when the id is numeric, else bold text."""
    from .mtproto_rich import rich_button

    label = " ".join(str(name or "").split()) or f"Customer {chat_id}"
    if str(chat_id).isdecimal():
        return rich_button(label, kind="profile", value=str(chat_id))
    return f"<b>{html.escape(label)}</b>"


def _chat_badge() -> str:
    """The small ``chat`` badge marking a chat-automation message."""
    from .mtproto_rich import rich_button

    return rich_button("chat", kind="callback", value="badge:chat", style="primary")


def _excerpt(text: Any, max_len: int = 120) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) > max_len:
        return flat[: max_len - 1].rstrip() + "…"
    return flat or "(no text)"


def _quote_block(text: str, *, max_len: int = 600) -> str:
    """Render a customer message as a quoted block for the owner DM."""
    if not text:
        return "  (no text)"
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return "\n".join(f"  > {line}" for line in text.splitlines())


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class BusinessModeManager:
    """Owns Business Mode state and orchestrates direct replies.

    One instance per Telegram adapter. The adapter wires up handlers in
    ``connect()`` and calls into this manager for every business update.
    """

    def __init__(
        self,
        session_db: Any,
        send_message: SendMessage,
        reply_via_turn: ReplyViaTurn,
        send_business_message: Optional[SendBusinessMessage] = None,
        debounce_seconds: float = 8.0,
        max_customer_text_chars: int = 4000,
        persona: Optional[str] = None,
        trigger_mode: str = "direct",
        trigger_word: str = "",
    ) -> None:
        self._db = session_db
        self._send = send_message
        self._reply_via_turn = reply_via_turn
        self._send_business = send_business_message
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._max_customer_text_chars = int(max_customer_text_chars)
        self._persona = DEFAULT_PERSONA if persona is None else str(persona)
        if trigger_mode not in TRIGGER_MODES:
            raise ValueError(f"invalid business trigger mode: {trigger_mode!r}")
        self._trigger_mode = trigger_mode
        self._trigger_word = str(trigger_word or "").strip()

        # In-flight debounce tasks, keyed by (connection_id, customer_chat_id).
        # New customer messages reset the timer so a typing burst yields one reply.
        self._debounce_tasks: Dict[str, asyncio.Task] = {}
        self._debounce_buffers: Dict[str, Dict[str, Any]] = {}

        # One reply-turn lock per (connection, customer chat): one turn at
        # a time per chat, never a global lock across chats.
        self._reply_locks: Dict[str, asyncio.Lock] = {}

        # Owner DMs in "next message = a new setting value" mode, one entry
        # per owner chat: "trigger_word" or "persona". Armed when the owner
        # taps the matching /biz control; the next DM is consumed as the
        # value (slash commands pass through — the capture never eats one).
        self._pending_owner_input: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # BusinessConnection updates (established / edited / ended).
    # ------------------------------------------------------------------

    async def handle_connection_update(self, connection: Any) -> None:
        """Persist a TL ``BotBusinessConnection`` update.

        Owner DM chat id IS ``connection.user_id`` (there is no
        ``user_chat_id`` on TL objects); ``is_enabled = not disabled``;
        the reply right is ``rights.reply``. Rights are stored as JSON
        for the /biz status display.
        """
        conn_id = getattr(connection, "connection_id", None)
        owner_user_id = getattr(connection, "user_id", None)
        if conn_id is None or owner_user_id is None:
            logger.warning(
                "BotBusinessConnection update missing required fields: "
                "connection_id=%s, user_id=%s",
                conn_id, owner_user_id,
            )
            return
        is_enabled = not bool(getattr(connection, "disabled", False))
        rights = _rights_dict(getattr(connection, "rights", None))
        can_reply = bool(rights.get("reply", False))
        owner_chat_id = str(owner_user_id)

        previous = self._db.get_telegram_business_connection(str(conn_id))
        self._db.upsert_telegram_business_connection(
            connection_id=str(conn_id),
            owner_user_id=str(owner_user_id),
            owner_chat_id=owner_chat_id,
            can_reply=can_reply,
            is_enabled=is_enabled,
            rights=rights,
        )

        # A live connection replaces every dead one for this owner — only
        # the active connection id stays (design §5).
        if is_enabled:
            removed = self.purge_dead_connections(str(owner_user_id))
            if removed:
                logger.debug("Purged %d dead business connection(s) for owner %s",
                             removed, owner_user_id)

        # First-time onboarding DM.
        if previous is None and is_enabled:
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text=ONBOARDING_MESSAGE,
                    disable_notification=False,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to deliver business-mode onboarding DM to %s: %s",
                    owner_chat_id, exc, exc_info=True,
                )

        # Connection ended → DM the owner a confirmation.
        if previous is not None and previous.get("is_enabled") and not is_enabled:
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="Business connection ended. I won't reply to customers on it any more.",
                    disable_notification=True,
                )
            except Exception as exc:
                logger.debug(
                    "Disconnection notice send failed (%s): %s",
                    owner_chat_id, exc, exc_info=True,
                )

        # Reply right changed → tell the owner.
        if previous is not None and previous.get("is_enabled") and is_enabled:
            if previous.get("can_reply") != can_reply:
                msg = (
                    "Send-on-your-behalf is on — I'll reply to customers directly."
                    if can_reply else
                    "Send-on-your-behalf is off in your Telegram Business "
                    "settings. I can't reply to customers until you turn it back on."
                )
                try:
                    await self._send(
                        chat_id=int(owner_chat_id), text=msg, disable_notification=True,
                    )
                except Exception:
                    logger.debug("can_reply flip notice send failed", exc_info=True)

    # ------------------------------------------------------------------
    # Business messages (customer talks to owner).
    # ------------------------------------------------------------------

    async def handle_business_message(
        self,
        connection_id: str,
        message: Any,
        *,
        reply_to_business_bot: bool = False,
        customer_name: Optional[str] = None,
    ) -> None:
        """Schedule a debounced direct reply for an incoming customer message.

        ``connection_id`` comes from the update (``Message`` carries no
        business-connection field). ``reply_to_business_bot`` is computed
        by the intake handler from the update's ``reply_to_message``
        (set when it replies to a business-bot-sent message) — it feeds
        the trigger gate, it is not re-derived here. ``customer_name``
        is the intake-resolved name when the adapter could resolve it;
        otherwise it is derived from the message (often just "Customer"
        on raw TL updates).
        """
        if not connection_id:
            return
        conn = self._db.get_telegram_business_connection(str(connection_id))
        if not conn:
            logger.debug("Received business_message for unknown connection %s", connection_id)
            return
        if not conn.get("is_enabled"):
            return
        if not conn.get("auto_draft", True):
            logger.debug("Business connection %s has auto_draft=False; skipping reply", connection_id)
            return

        # The guard (§3.2): only a message from the OTHER party in the chat
        # is a customer message. Outgoing ones come through the same update:
        # ones the owner typed carry the owner as from_id, and ones a bot
        # sent on the owner's behalf (including a direct reply) carry
        # via_business_bot_id. Return before the debounce so they cannot
        # cancel a pending reply for the customer's message.
        if getattr(message, "via_business_bot_id", None) is not None:
            return
        sender_id = _sender_id(message)
        if sender_id is not None and sender_id == str(conn.get("owner_user_id")):
            return

        # v2 handles private customer chats only; group business messages
        # are logged and ignored.
        if _is_group_chat(message):
            logger.debug("Ignoring group business message on connection %s", connection_id)
            return
        customer_chat_id = _customer_chat_id(message)
        if customer_chat_id is None:
            return
        name = (customer_name or "").strip() or _customer_display_name(message)
        customer_msg_id = _customer_msg_id(message)
        text = _message_text(message).strip()

        # Every genuine customer message lands in recent (the inbox view),
        # even one that will not reply (paused chat, no trigger match,
        # pure media without a caption, reply right off).
        self._db.record_business_message(
            str(connection_id), customer_chat_id, customer_msg_id,
            name, text, "in",
        )

        if customer_chat_id in (conn.get("paused_chats") or []):
            logger.debug("Customer chat %s paused; skipping reply", customer_chat_id)
            return
        if not conn.get("can_reply"):
            # No reply right: the turn would have nowhere to send. The
            # owner sees the message in Recent and the missing permission
            # in /biz status.
            logger.debug("Business connection %s cannot reply; skipping turn", connection_id)
            return
        if not text:
            # Pure media without caption — out of scope for v2 (no vision pipeline).
            return
        if len(text) > self._max_customer_text_chars:
            text = text[: self._max_customer_text_chars]

        # Trigger gate, evaluated BEFORE the debounce.
        if self._trigger_mode == "direct":
            trigger = "direct"
        elif reply_to_business_bot:
            trigger = "reply"
        else:
            trigger = "word"
        if not business_triggered(
            text, mode=self._trigger_mode, word=self._trigger_word,
            reply_to_business_bot=reply_to_business_bot,
        ):
            logger.debug(
                "Business message from %s did not match trigger mode %s; no reply",
                customer_chat_id, self._trigger_mode,
            )
            return

        key = f"{connection_id}:{customer_chat_id}"

        # Cancel any in-flight debounce for this (connection, customer chat) so
        # rapid bursts coalesce into a single reply against the latest text.
        prior = self._debounce_tasks.pop(key, None)
        if prior is not None and not prior.done():
            prior.cancel()

        # Buffer the latest message info — the timer will read this at fire time.
        self._debounce_buffers[key] = {
            "conn_id": str(connection_id),
            "owner_user_id": str(conn.get("owner_user_id")),
            "owner_chat_id": str(conn.get("owner_chat_id")),
            "customer_chat_id": str(customer_chat_id),
            "customer_msg_id": customer_msg_id or "",
            "customer_text": text,
            "customer_name": name,
            "trigger": trigger,
        }

        # Schedule the actual reply. If debounce is zero (test mode), fire
        # immediately so the test doesn't have to wait.
        if self._debounce_seconds <= 0:
            await self._run_reply(key)
        else:
            self._debounce_tasks[key] = asyncio.create_task(
                self._debounce_then_reply(key)
            )

    async def _debounce_then_reply(self, key: str) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self._debounce_seconds)
        except asyncio.CancelledError:
            return
        try:
            await self._run_reply(key)
        finally:
            # Only drop our own entry: a message that arrived while we were being
            # cancelled has already replaced the slot with its newer task, and
            # popping blindly deleted that task's handle — after which later
            # messages could no longer cancel it and an extra timer fired.
            if self._debounce_tasks.get(key) is task:
                self._debounce_tasks.pop(key, None)

    async def _run_reply(self, key: str) -> None:
        buf = self._debounce_buffers.pop(key, None)
        if not buf:
            return

        # Re-check the connection state — owner may have hit /biz pause
        # during the debounce window.
        conn = self._db.get_telegram_business_connection(buf["conn_id"])
        if not conn or not conn.get("is_enabled") or not conn.get("auto_draft", True):
            return
        if buf["customer_chat_id"] in (conn.get("paused_chats") or []):
            return
        if not conn.get("can_reply"):
            return

        lock = self._reply_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._reply_and_deliver(buf, conn)

    async def handle_owner_invocation(
        self,
        connection_id: str,
        customer_chat_id: str,
        text: str,
        customer_name: str,
    ) -> None:
        """Reply when the owner invokes the bot in a business chat.

        The owner's @mention arrives as a guest query (Telegram delivers
        it that way); the guest handler routes it here instead of
        answering as a guest DM. No trigger gate, no debounce — the owner
        explicitly asked. The turn runs on the same per-customer thread
        as a customer message, and the final text is direct-sent.
        """
        if not connection_id or not customer_chat_id:
            return
        conn = self._db.get_telegram_business_connection(str(connection_id))
        if not conn or not conn.get("is_enabled") or not conn.get("auto_draft", True):
            return
        if str(customer_chat_id) in (conn.get("paused_chats") or []):
            return
        if not conn.get("can_reply"):
            return
        text = (text or "").strip()
        if not text:
            return
        if len(text) > self._max_customer_text_chars:
            text = text[: self._max_customer_text_chars]
        buf = {
            "conn_id": str(connection_id),
            "owner_user_id": str(conn.get("owner_user_id")),
            "owner_chat_id": str(conn.get("owner_chat_id")),
            "customer_chat_id": str(customer_chat_id),
            "customer_msg_id": "",
            "customer_text": text,
            "customer_name": (customer_name or "").strip() or "Customer",
            "trigger": "owner",
            "owner_text": text,
        }
        key = f"{connection_id}:{customer_chat_id}"
        lock = self._reply_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._reply_and_deliver(buf, conn)

    async def _reply_and_deliver(self, buf: Dict[str, Any], conn: Dict[str, Any]) -> None:
        """Run the reply turn and direct-send its final text to the customer."""
        context = {
            "connection_id": buf["conn_id"],
            "customer_chat_id": buf["customer_chat_id"],
            "customer_name": buf["customer_name"],
            "trigger": buf["trigger"],
            "trigger_mode": self._trigger_mode,
            "trigger_word": self._trigger_word,
            "persona": self._persona,
            "owner_user_id": buf["owner_user_id"],
            "owner_chat_id": buf["owner_chat_id"],
            "recent_messages": self._recent_context(
                str(conn.get("owner_chat_id")), buf["customer_chat_id"],
            ),
        }
        if buf.get("owner_text") is not None:
            context["owner_text"] = buf["owner_text"]
        try:
            reply_text = await self._reply_via_turn(
                buf["customer_text"], buf["customer_chat_id"], context,
            )
        except Exception as exc:
            logger.exception("Business-mode reply turn failed: %s", exc)
            try:
                await self._send(
                    chat_id=int(buf["owner_chat_id"]),
                    text=(
                        "I couldn't reply to "
                        f"{buf['customer_name']}: {exc}.\n\n"
                        f"Their message was:\n\n{buf['customer_text']}"
                    ),
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Reply-failure notice send failed", exc_info=True)
            return

        reply_text = (reply_text or "").strip()
        if not reply_text:
            logger.debug("Empty reply for %s — skipping", buf["conn_id"])
            return

        try:
            await self._deliver_to_customer(
                buf["conn_id"], buf["customer_chat_id"], reply_text,
            )
        except Exception as exc:
            logger.warning(
                "Business send failed for %s: %s", buf["conn_id"], exc, exc_info=True,
            )
            try:
                await self._send(
                    chat_id=int(buf["owner_chat_id"]),
                    text=(
                        "I couldn't send the reply to "
                        f"{buf['customer_name']}: {exc}"
                    ),
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Send-failure notice send failed", exc_info=True)
            return

        self._db.record_business_message(
            buf["conn_id"], buf["customer_chat_id"], None,
            buf["customer_name"], reply_text, "out",
        )
        try:
            await self._send(
                chat_id=int(buf["owner_chat_id"]),
                text=self._render_sent_notice(
                    customer_name=buf["customer_name"],
                    customer_chat_id=buf["customer_chat_id"],
                    reply_text=reply_text,
                ),
                disable_notification=True,
            )
        except Exception:
            logger.debug("Sent notice send failed", exc_info=True)

    def _recent_context(self, owner_chat_id: str, customer_chat_id: str) -> List[Dict[str, Any]]:
        """Last 8 recent messages for one customer chat, oldest first."""
        rows = self._db.list_recent_business_messages(
            owner_chat_id, limit=8, customer_chat_id=customer_chat_id,
        )
        rows = list(reversed(rows))
        return [
            {
                "customer_name": r.get("customer_name") or "",
                "text": r.get("text") or "",
                "direction": r.get("direction") or "in",
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Business-message deletes (customer deleted their message).
    # ------------------------------------------------------------------

    async def handle_business_delete(
        self, connection_id: str, peer: Any, message_ids: Any,
    ) -> int:
        """A customer deleted their message. Drafts are gone in v2, so
        there is nothing to discard — the delete is consumed, nothing
        happens. Returns 0; the adapter keeps the call so the update is
        still consumed rather than dropped as unknown.
        """
        return 0

    # ------------------------------------------------------------------
    # Customer delivery (the one send path).
    # ------------------------------------------------------------------

    async def _deliver_to_customer(
        self, connection_id: str, customer_chat_id: str, text: str,
    ) -> None:
        """Send text to a customer — only ever via the injected callable."""
        if self._send_business is None:
            raise RuntimeError("business delivery is not configured")
        await self._send_business(
            connection_id=str(connection_id),
            customer_chat_id=str(customer_chat_id),
            text=text,
        )

    # ------------------------------------------------------------------
    # Inline-button callback dispatch (hm:biz:action / hm:biz:action:chat)
    # ------------------------------------------------------------------

    async def handle_callback(
        self,
        *,
        data: str,
        caller_user_id: Optional[str],
        answer: Callable[..., Awaitable[Any]],
    ) -> Any:
        """Handle a callback_query whose data starts with ``hm:biz:``.

        Returns the action/view name for the UI worker to render (the
        UI owns rendering), or True when the callback was answered here.
        Card actions return their name; mute/unmute apply the change and
        return ``"recent"`` so the UI re-renders the inbox; trigger_word
        and persona arm the owner-input capture and return their name so
        the UI can prompt for the new value.
        """
        if not isinstance(data, str) or not data.startswith(CALLBACK_PREFIX):
            return False

        rest = data[len(CALLBACK_PREFIX):]
        parts = rest.split(":")
        if len(parts) == 1:
            action = parts[0]
            if action not in CARD_ACTIONS:
                await answer(text="Unknown action.")
                return True
            if action in {"trigger_word", "persona"}:
                self._pending_owner_input[str(caller_user_id)] = action
            return action
        if len(parts) != 2:
            await answer(text="Invalid action.")
            return True
        action, chat_id = parts
        if action not in {"mute", "unmute"}:
            await answer(text="Unknown action.")
            return True
        connection_id = self._find_connection_for_chat(
            str(caller_user_id), chat_id)
        if connection_id is None:
            await answer(text="Unknown customer chat.")
            return True
        row = self._db.get_telegram_business_connection(connection_id)
        paused = list((row or {}).get("paused_chats") or [])
        if action == "mute":
            if chat_id not in paused:
                paused.append(chat_id)
        else:
            if chat_id in paused:
                paused.remove(chat_id)
        self._db.set_telegram_business_paused_chats(connection_id, paused)
        return "recent"

    def _find_connection_for_chat(
        self, owner_chat_id: str, customer_chat_id: str,
    ) -> Optional[str]:
        for row in self._db.list_recent_business_messages(
            owner_chat_id, limit=50, customer_chat_id=customer_chat_id,
        ):
            if str(row.get("customer_chat_id")) == str(customer_chat_id):
                return str(row.get("connection_id"))
        return None

    # ------------------------------------------------------------------
    # Owner-side "next message = a new setting value" capture.
    # ------------------------------------------------------------------

    async def maybe_handle_owner_input(
        self,
        *,
        owner_chat_id: str,
        text: str,
    ) -> Optional[Dict[str, Any]]:
        """If the owner just tapped a trigger-word/persona control, treat
        their next DM as the new value.

        Returns the settings changes to persist (``{}`` when the input was
        consumed but changed nothing — e.g. an empty message cancels the
        capture), or None when nothing was pending (the message was not
        consumed). The manager does NOT write config.yaml — the caller
        persists the returned changes.
        """
        owner_key = str(owner_chat_id)
        kind = self._pending_owner_input.pop(owner_key, None)
        if kind is None:
            return None

        value = (text or "").strip()
        if not value:
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="Cancelled — nothing changed.",
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Input-cancel notice send failed", exc_info=True)
            return {}

        if kind == "trigger_word":
            if re.search(r"\s", value):
                self._pending_owner_input[owner_key] = kind
                try:
                    await self._send(
                        chat_id=int(owner_chat_id),
                        text="The trigger word must be a single word — try again.",
                        disable_notification=True,
                    )
                except Exception:
                    logger.debug("Trigger-word retry notice send failed", exc_info=True)
                return {}
            try:
                result = self.set_trigger(word=value)
            except ValueError:
                self._pending_owner_input[owner_key] = kind
                return {}
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text=f"Trigger word set to {result.get('trigger_word')}.",
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Trigger-word confirmation send failed", exc_info=True)
            return {"trigger_word": result.get("trigger_word", "")}

        if kind == "persona":
            result = self.set_persona(value)
            try:
                await self._send(
                    chat_id=int(owner_chat_id),
                    text="Persona updated.",
                    disable_notification=True,
                )
            except Exception:
                logger.debug("Persona confirmation send failed", exc_info=True)
            return {"owner_persona": result.get("persona", value)}

        return {}

    # ------------------------------------------------------------------
    # Query / mutation helpers for the UI + tool workers (design §11).
    # ------------------------------------------------------------------

    def list_connections(
        self, *, owner_user_id: Optional[str] = None, enabled_only: bool = True,
    ) -> List[Dict[str, Any]]:
        return self._db.list_telegram_business_connections(
            owner_user_id=owner_user_id, enabled_only=enabled_only,
        )

    def purge_dead_connections(self, owner_user_id: str) -> int:
        """Delete disabled connections for an owner that has a live one.

        Read-time sweep (design §5): the owner's already-dead rows clear
        on the next /biz render without waiting for another connection
        update. Returns the number of rows removed. When no enabled
        connection exists the dead rows are the only trace — keep them.
        """
        live = self._db.list_telegram_business_connections(
            owner_user_id=str(owner_user_id), enabled_only=True)
        if not live:
            return 0
        return self._db.delete_disabled_connections_for_owner(str(owner_user_id))

    def recent_messages(
        self, owner_chat_id: str, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        return self._db.list_recent_business_messages(str(owner_chat_id), limit=limit)

    def set_auto_draft(self, connection_id: str, auto_draft: bool) -> None:
        self._db.set_telegram_business_auto_draft(
            str(connection_id), auto_draft=bool(auto_draft),
        )

    def set_paused_chats(self, connection_id: str, paused_chats: List[str]) -> None:
        self._db.set_telegram_business_paused_chats(
            str(connection_id), [str(c) for c in (paused_chats or [])],
        )

    def set_trigger(
        self, *, mode: Optional[str] = None, word: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set the trigger mode/word in memory and return them.

        The manager does NOT write config.yaml — the config worker
        persists the returned values.
        """
        if mode is not None:
            if mode not in TRIGGER_MODES:
                raise ValueError(f"invalid business trigger mode: {mode!r}")
            self._trigger_mode = mode
        if word is not None:
            cleaned = str(word).strip()
            if cleaned and re.search(r"\s", cleaned):
                raise ValueError("business trigger word must be a single word")
            self._trigger_word = cleaned
        return {"trigger_mode": self._trigger_mode, "trigger_word": self._trigger_word}

    def set_persona(self, persona: str) -> Dict[str, Any]:
        """Set the owner persona in memory and return it.

        The manager does NOT write config.yaml — the config worker
        persists the returned value.
        """
        self._persona = str(persona)
        return {"persona": self._persona}

    def get_settings(self) -> Dict[str, Any]:
        return {
            "trigger_mode": self._trigger_mode,
            "trigger_word": self._trigger_word,
            "persona": self._persona,
            "debounce_seconds": self._debounce_seconds,
            "max_customer_text_chars": self._max_customer_text_chars,
        }

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_sent_notice(
        *, customer_name: str, customer_chat_id: str, reply_text: str,
    ) -> str:
        """Minimal owner-DM notice after a direct send: who, chat badge, preview."""
        pill = _customer_pill(customer_name, customer_chat_id)
        badge = _chat_badge()
        return (
            f"<p>Sent to {pill} {badge} — "
            f"{html.escape(_excerpt(reply_text))}</p>"
        )
