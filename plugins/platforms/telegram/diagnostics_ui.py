"""Owner-only operational diagnostics in the existing Telethon feature UI path."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import secrets
import tempfile
import time
from types import SimpleNamespace
from typing import Any

from .mtproto_rich import inline_markup, send_request

logger = logging.getLogger(__name__)

_STATE_TTL = 15 * 60


class TelegramDiagnosticsUI:
    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._states: dict[str, dict[str, Any]] = {}

    def _home(self, source: Any) -> Path:
        return self.adapter._settings_ui()._home(source)

    def _is_owner(self, source: Any, home: Path) -> bool:
        """Ownership from the LAUNCH home's .env OWNER_TELEGRAM_ID alone — never
        the tenant store, never the chat type, never the runtime home: a profiled
        user owns their own home but host logs stay launch-owner-only (D7)."""
        if not getattr(source, "user_id", None):
            return False
        checker = getattr(self.adapter, "_is_launch_owner_user", None)
        if not callable(checker):
            return False
        return bool(checker(source.user_id, source))

    def _allowed(self, source: Any, home: Path) -> bool:
        if (str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct"}
                or not getattr(source, "user_id", None)):
            return False
        # Owner identity is the profile .env OWNER_TELEGRAM_ID alone. The
        # multiuser tenant store must never gate the owner: when it is down,
        # _authorized fails closed and the owner is locked out of /logs.
        return self._is_owner(source, home)

    @staticmethod
    def _is_private(source: Any) -> bool:
        return str(getattr(source, "chat_type", "")) in {"dm", "private", "direct"}

    def _prune(self) -> None:
        now = time.monotonic()
        self._states = {key: state for key, state in self._states.items()
                        if now - state["created"] < _STATE_TTL}

    async def open(self, source: Any, command: str, *, arguments: str = "",
                   reply_to: Any = None) -> None:
        home = self._home(source)
        if not self._is_owner(source, home):
            await self.adapter.send(source.chat_id,
                                    "Operational diagnostics are available only to the owner in a private chat.",
                                    reply_to=reply_to)
            return
        if not self._is_private(source):
            # The owner ran diagnostics in a shared chat. Log content never
            # lands there — the denial the owner kept hitting was the chat-type
            # gate, not the owner check. Acknowledge here and deliver the
            # diagnostics to the owner's DM instead.
            if command == "logs" and str(arguments or "").strip():
                # The forum-audit subcommand is addressed to the forum itself;
                # it stays DM-only.
                await self.adapter.send(source.chat_id,
                                        "Operational diagnostics are available only to the owner in a private chat.",
                                        reply_to=reply_to)
                return
            await self.adapter.send(source.chat_id,
                                    "Sending your diagnostics to your DM.",
                                    reply_to=reply_to)
            # Int ids: Telethon reads a digit-string peer as a phone number,
            # which bots cannot resolve (live ValueError on a cold entity
            # cache), so the hand-built source must carry ints like a real
            # SessionSource does.
            dm_source = SimpleNamespace(user_id=int(source.user_id),
                                        user_name=getattr(source, "user_name", None),
                                        chat_id=int(source.user_id), chat_type="dm",
                                        thread_id=None, is_bot=False)
            # The download state and the log export must be minted for the DM
            # delivery target's home (the launch home): the tap re-resolves
            # the home from the DM source, and a group source resolves to
            # the group profile home instead, so minting the group home
            # denies every group-triggered download at the tap. Resolve the
            # launch home directly: the hand-built dm_source has no
            # .profile/.platform, so the profile-home resolver crashes on
            # it (live: AttributeError inside its own except handler).
            from hermes_constants import get_hermes_home
            dm_home = Path(get_hermes_home()).expanduser().resolve()
            try:
                await self._deliver(dm_source, dm_home, command, arguments)
            except Exception:
                logger.exception("Could not deliver owner diagnostics to DM")
                await self.adapter.send(
                    source.chat_id,
                    "I couldn't reach your DM. Open a private chat with me and run /logs there.",
                    reply_to=reply_to)
            return
        await self._deliver(source, home, command, arguments, reply_to=reply_to)

    async def _deliver(self, source: Any, home: Path, command: str, arguments: str = "",
                       *, reply_to: Any = None) -> None:
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(home):
            if command == "logs" and str(arguments or "").strip():
                controller = getattr(self.adapter, "_forum_audit_transport", None)
                if controller is None:
                    await self.adapter.send(source.chat_id,
                                            "Private forum audit is unavailable on this adapter.",
                                            reply_to=reply_to)
                else:
                    await controller.command(source, arguments, reply_to=reply_to)
                return
            if command == "hf":
                from .hf_diagnostics import collect_hf_diagnostics
                text = await asyncio.to_thread(collect_hf_diagnostics, home, self.adapter)
                await self.adapter._mtproto_rich_request(send_request(
                    self.adapter._peer_for(source.chat_id), text,
                    reply_to=int(reply_to) if reply_to is not None else None,
                ))
                return
            from .diagnostic_logs import recent_logs
            try:
                excerpt = await asyncio.to_thread(recent_logs, home)
            except (OSError, ValueError) as exc:
                from agent.redact import redact_for_egress
                excerpt = "Log preview unavailable: " + redact_for_egress(str(exc))
            text = ("# Recent Hermes logs\n\n" + excerpt
                    + "\n\nLatest 25 lines per operational log (up to 5,000 characters each). "
                    "Download includes all retained managed logs and rotations, redacted; "
                    "it cannot recover deleted logs or unrecorded supervisor stdout.")
            self._prune()
            token = secrets.token_hex(12)
            state = {"home": str(home), "user": str(source.user_id), "chat": str(source.chat_id),
                     "thread": str(getattr(source, "thread_id", "") or ""),
                     "created": time.monotonic(), "message": None}
            # Bound memory and invalidate older cards for this identity/profile.
            self._states = {key: value for key, value in self._states.items()
                            if (value["home"], value["user"], value["chat"]) !=
                            (state["home"], state["user"], state["chat"])}
            markup = inline_markup([[("Download all retained logs (.zip)", f"dg:{token}", "primary")]])
            response = await self.adapter._mtproto_rich_request(send_request(
                self.adapter._peer_for(source.chat_id), text,
                reply_to=int(reply_to) if reply_to is not None else None, reply_markup=markup,
            ))
            state["message"] = str(self.adapter._sent_message_id(response) or "")
            if state["message"]:
                self._states[token] = state

    async def handle_callback(self, event: Any, source: Any, data: str) -> None:
        self._prune()
        state = self._states.get(data.partition(":")[2])
        home = self._home(source)
        valid = state and self._allowed(source, home) and (
            state["home"], state["user"], state["chat"], state["thread"], state["message"]
        ) == (str(home), str(source.user_id), str(source.chat_id),
              str(getattr(source, "thread_id", "") or ""), str(getattr(event, "message_id", "") or ""))
        if not valid:
            await event.answer("This log download expired or is not authorized for this account.", alert=True)
            return
        if state.get("busy"):
            await event.answer("A log export is already in progress.")
            return
        state["busy"] = True
        await event.answer("Preparing redacted retained logs…")
        from gateway.run import _profile_runtime_scope
        from .diagnostic_logs import export_logs
        try:
            with _profile_runtime_scope(home):
                with tempfile.TemporaryDirectory(prefix="hermes-log-export-") as directory:
                    archive = Path(directory) / "hermes-logs-redacted.zip"
                    count = await asyncio.to_thread(export_logs, home, archive)
                    result = await self.adapter.send_document(
                        source.chat_id, str(archive),
                        caption=f"{count} retained Hermes log files, redacted. Review before sharing.",
                        reply_to=state["message"],
                    )
                    if not result.success:
                        await self.adapter.send(source.chat_id, "The log export could not be delivered. Please try again.")
        except (OSError, ValueError):
            await self.adapter.send(source.chat_id,
                                    "The log export could not be safely read or redacted. No raw files were sent; check local log permissions and export size.")
        finally:
            state["busy"] = False
