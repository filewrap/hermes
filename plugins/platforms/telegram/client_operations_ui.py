"""Telegram client operations presentation and payload-bound confirmation UI.

Integration API: ``TelegramClientOperationsUI(adapter).request(source, action, params)``;
``handle_command(message, source, command_name, arguments, reply_to=None)``;
``handle_callback(event, source, data)``. No command registration or adapter edits live here.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
import shlex
import time
from typing import Any

from .client_operations import TelegramClientOperations, MUTATION_ACTIONS
from .mtproto_rich import edit_request, inline_markup, send_request

logger = logging.getLogger(__name__)

CONFIRM_TTL = 180.0
PAGE_SIZE = 8
DOWNLOAD_PROGRESS_EDIT_INTERVAL = 3.0


class TelegramClientOperationsUI:
    """Render operation results and enforce owner-confirmed, callback-bound mutations."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self.operations = TelegramClientOperations(adapter)
        self._states: dict[str, dict[str, Any]] = {}

    async def request(self, source: Any, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Agent-tool API: current chat only, with all mutations deferred to UI confirmation."""
        return await self._request(source, action, params, render=False)

    async def _request(self, source: Any, action: str, params: dict[str, Any] | None = None,
                       *, render: bool, reply_to: Any = None) -> dict[str, Any]:
        if str(action or "").strip().lower() == "files_download":
            return await self._download_request(source, params, reply_to=reply_to)
        result = await self.operations.request(source, action, params)
        if str(action or "").strip().lower() == "files_metadata":
            result = self._file_target_result(result)
        if result.get("status") == "confirmation_required":
            token = secrets.token_hex(8)
            state = self._new_state(source, token, "confirm", action=result["action"], params=result["params"])
            self._states[token] = state
            text = self._confirmation_text(result["action"], result["params"], source)
            markup = inline_markup([[('Confirm exact action', f'tgo:c:{token}:yes', 'danger'),
                                     ('Cancel', f'tgo:c:{token}:no', None)]])
            msg_id = await self._send_card(source, text, markup, reply_to=reply_to)
            if not msg_id:
                self._states.pop(token, None)
                return {"ok": False, "status": "error", "error": {
                    "code": "CONFIRMATION_DELIVERY_FAILED", "message": "The confirmation card could not be delivered; no action was performed."}}
            state["message_id"] = str(msg_id)
            return {**result, "confirmation": {"required": True, "expires_in_seconds": int(CONFIRM_TTL),
                                                  "callback_prefix": "tgo:", "message_id": str(msg_id)}}
        if render and result.get("status") == "complete" or render and result.get("status") == "error":
            await self._show_result(source, result, reply_to=None)
        return result

    def observe(self, source: Any, message: Any) -> None:
        """Forward an already-admitted inbound/outbound message into the bounded local cache."""
        self.operations.observe(source, message)

    async def handle_command(self, message: Any, source: Any, command_name: str,
                             arguments: str, reply_to: Any = None) -> bool:
        """Handle only the six dedicated Telegram operations commands."""
        name = str(command_name or "").strip().lstrip("/").lower().split("@", 1)[0]
        text = str(arguments or "").strip()
        try:
            action, params = self._parse_command(name, text)
        except (ValueError, IndexError) as exc:
            await self._show_result(source, {"ok": False, "status": "error", "error": {
                "code": "INVALID_ARGUMENT", "message": str(exc)}}, reply_to=reply_to)
            return True
        if action is None:
            return False
        if action in {"files_metadata", "files_download"} and not params.get("message_id"):
            target_id, error = self._reply_file_target(message, action)
            if error is not None:
                await self._show_result(source, error, reply_to=reply_to)
                return True
            params["message_id"] = target_id
        result = await self._request(source, action, params, render=False, reply_to=reply_to)
        if result.get("status") != "confirmation_required":
            if action != "files_download":
                await self._show_result(source, result, reply_to=reply_to)
        return True

    @staticmethod
    def _parse_command(name: str, text: str) -> tuple[str | None, dict[str, Any]]:
        if name not in {"poll", "tgmessages", "tgfiles", "tginfo", "tgadmin", "tgprofile"}:
            return None, {}
        if name == "poll":
            command, _, rest = text.partition(" ")
            command = command.lower()
            if command == "create":
                parts = [part.strip() for part in rest.split("|")]
                if len(parts) < 3:
                    raise ValueError("Usage: /poll create question | option 1 | option 2")
                return "poll_create", {"question": parts[0], "options": parts[1:]}
            if command in {"results", "stop"}:
                msg_id = rest.strip()
                if command == "results":
                    return "poll_results", {"message_id": msg_id}
                return "poll_stop", {"message_id": msg_id}
            raise ValueError("Use /poll create question | option 1 | option 2, /poll results <message ID>, or /poll stop <message ID>.")
        if name == "tgmessages":
            command, _, rest = text.partition(" ")
            command = command.lower() or "history"
            if command == "history":
                return "messages_history", {"limit": int(rest.strip() or "20")}
            if command == "search":
                return "messages_search", {"query": rest.strip()}
            if command == "get":
                return "message_get", {"message_id": rest.strip()}
            raise ValueError("Use /tgmessages [history [limit] | search <text> | get <message ID>].")
        if name == "tgfiles":
            command, _, rest = text.partition(" ")
            command = command.lower()
            if not command:
                return "files_metadata", {"message_id": ""}
            if command not in {"metadata", "download"}:
                raise ValueError("Reply to a photo, video, audio, or file with /tgfiles to inspect it; or use /tgfiles metadata <message ID> or /tgfiles download <message ID>.")
            return ("files_metadata" if command == "metadata" else "files_download",
                    {"message_id": rest.strip()})
        if name == "tginfo":
            if text.lower() in {"topics", "topic"}:
                return "chat_topics", {}
            if text.lower() in {"user", "me"}:
                return "user_info", {}
            if text.lower().startswith("user "):
                return "user_info", {"user_id": text.partition(" ")[2].strip()}
            if text.lower() in {"bot", "profile"}:
                return "bot_profile", {}
            if text:
                raise ValueError("Use /tginfo [topics | user [visible user ID] | bot].")
            return "chat_info", {}
        if name == "tgadmin":
            command, _, rest = text.partition(" ")
            command = command.lower()
            if command == "title":
                return "chat_title", {"title": rest}
            if command == "about":
                return "chat_about", {"about": rest}
            if command in {"ban", "unban"}:
                return ("user_ban" if command == "ban" else "user_unban", {"user_id": rest.strip()})
            if command == "delete":
                try:
                    ids = [part for part in rest.replace(",", " ").split()]
                except Exception as exc:
                    raise ValueError("Use /tgadmin delete <message ID> [message ID ...].") from exc
                return "messages_delete", {"message_ids": ids}
            if command == "permissions":
                denied = [x.strip() for x in rest.split(",") if x.strip()]
                if not denied:
                    raise ValueError("Use /tgadmin permissions <denied,right,names>.")
                return "chat_permissions", {"denied": {key: True for key in denied}}
            raise ValueError("Use /tgadmin title|about|ban|unban|delete|permissions with its payload.")
        if name == "tgprofile":
            fields = {}
            try:
                tokens = shlex.split(text)
            except ValueError as exc:
                raise ValueError("Profile fields must be key=value pairs.") from exc
            for token in tokens:
                key, sep, value = token.partition("=")
                if not sep or key not in {"name", "about", "description"}:
                    raise ValueError("Use /tgprofile name=... about=... description=... .")
                fields[key] = value
            return "bot_profile_update", fields
        return None, {}

    @staticmethod
    def _reply_file_target(message: Any, action: str) -> tuple[str | None, dict[str, Any] | None]:
        """Resolve only the command's native reply header, never its own delivery ID."""
        raw_message = getattr(message, "raw_message", None) or message
        reply_id = getattr(raw_message, "reply_to_msg_id", None)
        if reply_id is None:
            reply_header = getattr(raw_message, "reply_to", None)
            reply_id = getattr(reply_header, "reply_to_msg_id", None)
        if reply_id is None or str(reply_id).strip() in {"", "0"}:
            verb = "download" if action == "files_download" else "metadata"
            return None, {"ok": False, "status": "error", "error": {
                "code": "FILE_TARGET_MISSING",
                "message": f"No media reply was found. Reply directly to a photo, video, audio, or file and send /tgfiles {verb}, or provide its ID with /tgfiles metadata <message ID> or /tgfiles download <message ID>."}}
        try:
            target_id = int(reply_id)
        except (TypeError, ValueError):
            return None, {"ok": False, "status": "error", "error": {
                "code": "FILE_TARGET_INVALID", "message": "Telegram's replied-to message ID is invalid. Reply directly to the media again, or use its positive numeric message ID."}}
        if target_id <= 0:
            return None, {"ok": False, "status": "error", "error": {
                "code": "FILE_TARGET_INVALID", "message": "Telegram's replied-to message ID is invalid. Reply directly to the media again, or use its positive numeric message ID."}}
        return str(target_id), None

    @staticmethod
    def _file_target_result(result: dict[str, Any]) -> dict[str, Any]:
        """Turn low-level file-target failures into clear next steps for chat users."""
        if result.get("ok"):
            return result
        error = dict(result.get("error") or {})
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        lowered = message.lower()
        if "no accessible media" in lowered or "has no accessible media" in lowered:
            error["message"] = ("That message has no accessible media. Reply to a photo, video, audio, or file message, "
                                "or choose the ID of a message that contains media.")
        elif "does not belong to the current telegram chat" in lowered or "does not belong to the current telegram topic" in lowered:
            error["message"] = ("That ID is outside the current Telegram chat/topic. Use a media ID from this exact chat/topic, "
                                "or reply directly to the media there.")
        elif code in {"INVALID_ARGUMENT", "MESSAGEIDINVALIDERROR", "MESSAGE_ID_INVALID"} or "must be an integer" in lowered:
            error["message"] = ("That message ID is invalid or unavailable. Use a positive numeric ID from this chat/topic, "
                                "or reply directly to the media and retry /tgfiles.")
        return {**result, "error": error}

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not str(data or "").startswith("tgo:"):
            return False
        parts = str(data).split(":")
        if len(parts) not in {3, 4}:
            await self._answer(event, "This Telegram operation button is invalid.")
            return True
        _, kind, token, *tail = parts
        bound = self._bound_state(event, source, token)
        if bound is None:
            await self._answer(event, "This card expired or belongs to another chat, account, or profile.")
            return True
        state, source = bound
        if kind == "d" and len(tail) == 1 and tail[0] == "cancel":
            if state.get("kind") != "download" or not state.get("running"):
                await self._answer(event, "This download is no longer active.")
                return True
            cancel_event = state["cancel_event"]
            if cancel_event.is_set():
                await self._answer(event, "Download cancellation is already requested.")
                return True
            cancel_event.set()
            await self._answer(event, "Download cancellation requested.")
            return True
        if kind == "c" and state["kind"] == "confirm" and len(tail) == 1:
            choice = tail[0]
            # Consume synchronously before any await, so overlapping button taps cannot replay it.
            self._states.pop(token, None)
            if choice == "no":
                await self._answer(event, "Cancelled; no Telegram change was made.")
                await self._edit_card(state, "<b>Cancelled</b>\n\nNo Telegram change was made.\n<i>Page 1 of 1 • total 0</i>", None)
                return True
            if choice != "yes":
                await self._answer(event, "This confirmation action is unavailable.")
                return True
            await self._answer(event, "Rechecking authorization and applying the confirmed payload.")
            if self.operations._profile(source) != state["profile"]:
                result = self.operations._error("PROFILE_CHANGED", "The Telegram profile changed; request a new confirmation.")
            else:
                result = await self.operations.execute_confirmed(source, state["action"], state["params"])
            await self._edit_card(state, self._render_result(result, page=0)[0], None)
            return True
        if kind == "p" and state["kind"] == "pages" and len(tail) == 1:
            try:
                page = int(tail[0])
            except ValueError:
                page = -1
            if page < 0 or page >= state["page_count"]:
                await self._answer(event, "That result page is unavailable.")
                return True
            await self._answer(event)
            text, markup = self._render_result(state["result"], page=page, token=token)
            await self._edit_card(state, text, markup)
            return True
        await self._answer(event, "This Telegram operation button is unavailable.")
        return True

    callback = handle_callback

    def _new_state(self, source: Any, token: str, kind: str, **extra: Any) -> dict[str, Any]:
        now = time.monotonic()
        self._states = {key: value for key, value in self._states.items()
                        if value.get("running") or now - value["created_at"] <= CONFIRM_TTL}
        while len(self._states) >= 256:
            removable = next((key for key, value in self._states.items() if not value.get("running")), None)
            if removable is None:
                break
            self._states.pop(removable, None)
        return {"token": token, "kind": kind, "actor_id": str(getattr(source, "user_id", "") or ""),
                "chat_id": str(getattr(source, "chat_id", "") or ""),
                "thread_id": str(getattr(source, "thread_id", "") or ""),
                "profile": self.operations._profile(source), "created_at": time.monotonic(),
                "message_id": None, **extra}

    def _bound_state(self, event: Any, source: Any, token: str) -> tuple[dict[str, Any], Any] | None:
        now = time.monotonic()
        for key, value in list(self._states.items()):
            if not value.get("running") and now - value["created_at"] > CONFIRM_TTL:
                self._states.pop(key, None)
        state = self._states.get(token)
        if not state or state.get("message_id") != str(getattr(event, "message_id", "") or ""):
            return None
        try:
            profile = self.operations._profile(source)
        except Exception:
            return None
        matches = (state["actor_id"] == str(getattr(source, "user_id", "") or "")
                   and state["chat_id"] == str(getattr(source, "chat_id", "") or "")
                   and state["profile"] == profile)
        current_thread = str(getattr(source, "thread_id", "") or "")
        if matches and not current_thread and state["thread_id"]:
            # Callback updates can omit their topic ID. Restore it only from this exact
            # callback message's already-bound state, preserving the gateway identity.
            try:
                from gateway.session_identity import replace_source
                source = replace_source(source, thread_id=state["thread_id"])
                current_thread = str(getattr(source, "thread_id", "") or "")
            except Exception:
                return None
        matches = matches and state["thread_id"] == current_thread
        if not matches:
            return None
        # Authorization is rechecked at callback time. Confirmations also recheck again in
        # execute_confirmed immediately before any MTProto side effect.
        admitted, _ = self.operations._authorize(source, owner=state["kind"] == "confirm")
        return (state, source) if admitted else None

    async def _answer(self, event: Any, text: str = "") -> None:
        answer = getattr(event, "answer", None)
        if callable(answer):
            try:
                await answer(text or None, alert=bool(text and "expired" in text.lower()))
            except Exception as exc:
                logger.debug("Could not answer Telegram operations callback (%s)", type(exc).__name__)

    async def _send_card(self, source: Any, text: str, markup: Any, reply_to: Any = None) -> str | None:
        try:
            peer = await self.adapter._input_peer_for(str(source.chat_id))
            reply = reply_to or (getattr(source, "message_id", None))
            thread = getattr(source, "thread_id", None)
            request = send_request(
                peer, text, reply_to=int(reply) if reply and str(reply).isdigit() else (int(thread) if thread else None),
                top_msg_id=int(thread) if thread else None, reply_markup=markup, rich_format="html")
            response = await self.adapter._mtproto_rich_request(request)
            from .topic_delivery import sent_message_id
            return sent_message_id(response, random_id=request.random_id, peer=peer,
                                   topic_id=int(thread) if thread else None)
        except Exception as exc:
            logger.warning("Could not deliver Telegram client-operations card (%s)", type(exc).__name__)
            return None

    async def _download_request(self, source: Any, params: dict[str, Any] | None,
                                *, reply_to: Any = None) -> dict[str, Any]:
        """Show one cancellable progress card around an explicit MTProto file download."""
        admitted, _ = self.operations._authorize(source, owner=False)
        if not admitted:
            return await self.operations.request(source, "files_download", params)
        token = secrets.token_hex(8)
        try:
            state = self._new_state(source, token, "download", running=True,
                                    cancel_event=asyncio.Event(), edit_lock=asyncio.Lock(),
                                    last_update=0.0, started_at=None, last_downloaded_bytes=0,
                                    pending_progress=None, progress_task=None,
                                    progress_disabled=False)
        except Exception:
            return self.operations._error("PROFILE_UNAVAILABLE", "The Telegram profile could not be resolved safely.")
        self._states[token] = state
        try:
            message_id = await self._send_card(source, self._download_status_text("Preparing explicit Telegram media download."),
                                               self._download_cancel_markup(token), reply_to=reply_to)
        except asyncio.CancelledError:
            state["running"] = False
            self._states.pop(token, None)
            raise
        if not message_id:
            state["running"] = False
            self._states.pop(token, None)
            return {"ok": False, "status": "error", "error": {
                "code": "DOWNLOAD_CARD_DELIVERY_FAILED",
                "message": "The download status card could not be delivered; the download was not started."}}
        state["message_id"] = str(message_id)
        if not self._download_scope_valid(source, state):
            state["running"] = False
            self._states.pop(token, None)
            return self.operations._error("DOWNLOAD_SCOPE_CHANGED", "The download scope changed while delivering its status card; no download was started.")

        async def report_progress(downloaded: int, total: int | None) -> None:
            if not state.get("running") or state["cancel_event"].is_set():
                return
            downloaded = max(0, int(downloaded))
            state["last_downloaded_bytes"] = downloaded
            if state.get("progress_disabled"):
                return
            # Operations validates authorization/profile on every part already.
            # Resolve it again immediately before an external edit, not for every
            # coalesced byte snapshot (the resolver may perform database I/O).
            # A single worker consumes the most recent snapshot. Replacing this value
            # coalesces arbitrarily frequent callbacks without queueing or blocking I/O.
            state["pending_progress"] = (downloaded, total)
            task = state.get("progress_task")
            if task is None or task.done():
                state["progress_task"] = asyncio.create_task(
                    self._run_download_progress(source, state, token))

        state["started_at"] = time.monotonic()
        try:
            result = await self.operations.request(source, "files_download", params,
                                                   progress_callback=report_progress,
                                                   cancel_event=state["cancel_event"])
        except asyncio.CancelledError:
            state["running"] = False
            self._states.pop(token, None)
            try:
                await self._stop_download_progress(state)
                async with state["edit_lock"]:
                    if self._download_scope_valid(source, state):
                        elapsed = max(0.0, time.monotonic() - (state.get("started_at") or time.monotonic()))
                        downloaded = int(state.get("last_downloaded_bytes") or 0)
                        average = downloaded / elapsed if elapsed > 0 else 0.0
                        metrics = (f"Elapsed: {elapsed:.1f} s · Average throughput: "
                                   f"{self._format_throughput(average)}")
                        await self._edit_card(state, self._download_status_text(
                            "Download task cancelled.", metrics=metrics), None)
            finally:
                raise
        except BaseException:
            state["running"] = False
            self._states.pop(token, None)
            await self._stop_download_progress(state)
            raise
        state["running"] = False
        self._states.pop(token, None)
        await self._stop_download_progress(state)
        result = self._file_target_result(result)
        elapsed = max(0.0, time.monotonic() - (state.get("started_at") or time.monotonic()))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        measured_elapsed = data.get("elapsed_seconds", result.get("elapsed_seconds"))
        if isinstance(measured_elapsed, (int, float)) and measured_elapsed >= 0:
            elapsed = float(measured_elapsed)
        measured_average = data.get("average_bytes_per_second", result.get("average_bytes_per_second"))
        average = (float(measured_average) if isinstance(measured_average, (int, float)) and measured_average >= 0
                   else (int(data.get("downloaded_bytes") or state.get("last_downloaded_bytes") or 0) / elapsed
                         if elapsed > 0 else 0.0))
        async with state["edit_lock"]:
            if self._download_scope_valid(source, state):
                await self._edit_card(state, self._render_download_result(result, elapsed, average), None)
        return result

    async def _run_download_progress(self, source: Any, state: dict[str, Any], token: str) -> None:
        """Edit the progress card serially, honoring the throttle and latest snapshot."""
        current_task = asyncio.current_task()
        try:
            while state.get("running") and not state["cancel_event"].is_set():
                snapshot = state.get("pending_progress")
                if snapshot is None:
                    return
                delay = DOWNLOAD_PROGRESS_EDIT_INTERVAL - (time.monotonic() - state["last_update"])
                if delay > 0:
                    await asyncio.sleep(delay)
                if (not state.get("running") or state["cancel_event"].is_set()
                        or state.get("progress_disabled")):
                    return
                if not self._download_scope_valid(source, state):
                    state["progress_disabled"] = True
                    state["pending_progress"] = None
                    return
                # A callback arriving during the throttle wait supersedes the old snapshot.
                snapshot = state.get("pending_progress")
                if snapshot is None:
                    return
                downloaded, total = snapshot
                state["pending_progress"] = None
                async with state["edit_lock"]:
                    if (not state.get("running") or state["cancel_event"].is_set()
                            or state.get("progress_disabled")
                            or not self._download_scope_valid(source, state)):
                        return
                    now = time.monotonic()
                    # The lock can have been held by another card action while we waited.
                    delay = DOWNLOAD_PROGRESS_EDIT_INTERVAL - (now - state["last_update"])
                    if delay > 0:
                        state["pending_progress"] = (downloaded, total)
                        await asyncio.sleep(delay)
                        continue
                    state["last_update"] = now
                    elapsed = max(0.0, now - (state.get("started_at") or now))
                    average = downloaded / elapsed if elapsed > 0 else 0.0
                    await self._edit_card(state, self._download_progress_text(downloaded, total, elapsed, average),
                                          self._download_cancel_markup(token))
        finally:
            if state.get("progress_task") is current_task:
                state["progress_task"] = None

    @staticmethod
    async def _stop_download_progress(state: dict[str, Any]) -> None:
        """Drop pending snapshots and cancel/gather the sole progress edit worker."""
        state["pending_progress"] = None
        task = state.get("progress_task")
        if task is None:
            return
        state["progress_task"] = None
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
        if task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    def _download_scope_valid(self, source: Any, state: dict[str, Any]) -> bool:
        try:
            same_scope = (state["actor_id"] == str(getattr(source, "user_id", "") or "")
                          and state["chat_id"] == str(getattr(source, "chat_id", "") or "")
                          and state["thread_id"] == str(getattr(source, "thread_id", "") or "")
                          and state["profile"] == self.operations._profile(source))
            admitted, _ = self.operations._authorize(source, owner=False)
            return same_scope and admitted
        except Exception:
            return False

    @staticmethod
    def _download_cancel_markup(token: str) -> Any:
        return inline_markup([[('Cancel download', f'tgo:d:{token}:cancel', None)]])

    @staticmethod
    def _download_status_text(message: str, *, metrics: str | None = None) -> str:
        metrics_line = f"\n{metrics}" if metrics else ""
        payload = html.escape(f"{message}{metrics_line}")
        return f"<b>Telegram media download</b>\n<pre><code>{payload}</code></pre>"

    @classmethod
    def _download_progress_text(cls, downloaded: int, total: int | None,
                                elapsed_seconds: float = 0.0,
                                average_bytes_per_second: float = 0.0) -> str:
        downloaded = max(0, int(downloaded))
        if total is None or int(total) <= 0:
            progress = (f"Downloaded {cls._format_download_amount(downloaded)}; "
                        "total size is not available.")
        else:
            total = int(total)
            progress = (f"Downloaded {cls._format_download_amount(downloaded)}\n"
                        f"of {cls._format_download_amount(total)} ({downloaded * 100 / total:.1f}%).")
        metrics = (f"Elapsed: {max(0.0, float(elapsed_seconds)):.1f} s\nAverage throughput: "
                   f"{cls._format_throughput(average_bytes_per_second)}")
        return cls._download_status_text(progress, metrics=metrics)

    @staticmethod
    def _format_decimal_megabytes(value: float) -> str:
        """Format bytes using decimal megabytes (1 MB = 1,000,000 bytes)."""
        megabytes = max(0.0, float(value)) / 1_000_000
        precision = 2 if megabytes >= 1 else 6
        return f"{megabytes:,.{precision}f} MB"

    @classmethod
    def _format_download_amount(cls, value: int) -> str:
        amount = max(0, int(value))
        return cls._format_decimal_megabytes(amount)

    @classmethod
    def _format_throughput(cls, value: float) -> str:
        return f"{cls._format_decimal_megabytes(value)}/s"

    @staticmethod
    def _render_download_result(result: dict[str, Any], elapsed_seconds: float | None = None,
                                average_bytes_per_second: float | None = None) -> str:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        elapsed = data.get("elapsed_seconds", result.get("elapsed_seconds", elapsed_seconds))
        average = data.get("average_bytes_per_second", result.get("average_bytes_per_second", average_bytes_per_second))
        elapsed = max(0.0, float(elapsed or 0.0))
        average = max(0.0, float(average or 0.0))
        metrics = (f"\n<i>Elapsed: {elapsed:.1f} s · Average throughput: "
                   f"{TelegramClientOperationsUI._format_throughput(average)}</i>")
        if result.get("ok"):
            title = "Telegram media download complete"
            downloaded = data.get("downloaded_bytes")
            size = data.get("file_size")
            summary_lines = []
            if isinstance(downloaded, (int, float)) and downloaded >= 0:
                summary_lines.append(f"Downloaded: {TelegramClientOperationsUI._format_download_amount(downloaded)}")
            if (isinstance(size, (int, float)) and size > 0
                    and (downloaded is None or int(size) != int(downloaded))):
                summary_lines.append(f"Media size: {TelegramClientOperationsUI._format_download_amount(size)}")
            summary = f"<b>Transfer summary</b>\n{html.escape(chr(10).join(summary_lines))}\n" if summary_lines else ""
            # Keep machine fields useful to callers without exposing profile paths or
            # internal transfer policy/telemetry in a chat card.
            display_data = {key: value for key, value in data.items()
                            if key not in {"cached_path", "cache_path", "path", "download_diagnostics", "download_limits"}}
            payload = json.dumps(display_data, ensure_ascii=False, indent=2, default=str)
        else:
            error = result.get("error") or {}
            code = str(error.get("code", "ERROR"))
            title = "Telegram media download cancelled" if code == "DOWNLOAD_CANCELLED" else "Telegram media download failed"
            payload = f"{code}: {error.get('message', 'Download failed')}"
        return f"<b>{title}</b>\n{summary if result.get('ok') else ''}<pre><code>{html.escape(payload)}</code></pre>{metrics}"

    async def _edit_card(self, state: dict[str, Any], text: str, markup: Any) -> None:
        try:
            peer = await self.adapter._input_peer_for(state["chat_id"])
            await self.adapter._mtproto_rich_request(edit_request(peer, int(state["message_id"]), text,
                                                                   reply_markup=markup, rich_format="html"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Could not edit Telegram client-operations card (%s)", type(exc).__name__)

    async def _show_result(self, source: Any, result: dict[str, Any], reply_to: Any = None) -> None:
        text, markup = self._render_result(result, page=0)
        data = result.get("data")
        rows = self._rows(data)
        if result.get("ok") and rows and len(rows) > PAGE_SIZE:
            token = secrets.token_hex(8)
            state = self._new_state(source, token, "pages", result=result,
                                    page_count=(len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
            self._states[token] = state
            text, markup = self._render_result(result, page=0, token=token)
            state["message_id"] = await self._send_card(source, text, markup, reply_to=reply_to)
            if not state["message_id"]:
                self._states.pop(token, None)
            return
        await self._send_card(source, text, markup, reply_to=reply_to)

    @staticmethod
    def _rows(data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("messages", "topics", "answers"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def _render_result(self, result: dict[str, Any], page: int = 0, token: str | None = None) -> tuple[str, Any]:
        if not result.get("ok"):
            error = result.get("error") or {}
            payload = html.escape(f"{error.get('code', 'ERROR')}: {error.get('message', 'Operation failed')}")
            return f"<b>Telegram operation failed</b>\n<pre><code>{payload}</code></pre>\n<i>Page 1 of 1 • total 0</i>", None
        data = result.get("data")
        action = html.escape(str(result.get("action", "Telegram operation")))
        rows = self._rows(data)
        if rows:
            total = int(data.get("total", len(rows))) if isinstance(data, dict) else len(rows)
            page_count = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, page_count - 1))
            chosen = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
            rendered = json.dumps(chosen, ensure_ascii=False, indent=2, default=str)
            body = f"<b>{action}</b>\n<pre><code>{html.escape(rendered)}</code></pre>\n<i>Page {page + 1} of {page_count} • total {total}</i>"
            buttons = []
            if token and page_count > 1:
                row = []
                if page > 0:
                    row.append((f"Previous (Page {page})", f"tgo:p:{token}:{page - 1}", None))
                if page + 1 < page_count:
                    row.append((f"Next (Page {page + 2})", f"tgo:p:{token}:{page + 1}", "primary"))
                buttons.append(row)
            return body, inline_markup(buttons)
        rendered = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        total = 0 if data is None else 1
        return f"<b>{action}</b>\n<pre><code>{html.escape(rendered)}</code></pre>\n<i>Page 1 of 1 • total {total}</i>", None

    @staticmethod
    def _confirmation_text(action: str, params: dict[str, Any], source: Any = None) -> str:
        payload = html.escape(json.dumps({"action": action, "payload": params}, ensure_ascii=False, indent=2))
        target = (f"Chat <code>{html.escape(str(source.chat_id))}</code> · topic "
                  f"<code>{html.escape(str(getattr(source, 'thread_id', '') or 'none'))}</code> · profile "
                  f"<code>{html.escape(str(getattr(source, 'profile', '') or 'default'))}</code>\n") if source is not None else ""
        return ("<b>Confirm Telegram change</b>\n"
                + target +
                ("This replaces all default restrictions; omitted rights become unrestricted.\n" if action == "chat_permissions" else "") +
                "This exact action will run only if the owner confirms within three minutes.\n"
                f"<pre><code>{payload}</code></pre>\n<i>Page 1 of 1 • total 1</i>")
