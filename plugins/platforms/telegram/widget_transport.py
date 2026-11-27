"""Widget transport for the Telegram MTProto adapter.

Kept out of ``telethon_adapter.py`` (already over the file-size cap): the
layer-233 widget send/upload/answer/edit/read calls live here as a mixin;
the adapter only wires it into its bases, so callers keep using
``adapter.send_widget`` and friends. Builders and validation live in
``widget_messages.py``; the tap loop lives in ``widget_queries.py``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)


class TelegramWidgetTransportMixin:
    """Send/upload/answer/edit/read for layer-233 Telegram widgets."""

    async def _widget_peer_or_refusal(self, chat_id: Any, *,
                                      require_dm: bool = False) -> tuple[Any | None, Optional[SendResult]]:
        """Resolve a widget destination peer, or the refusal for a channel.

        Channels receive widgets only as forwards; supergroups are channels
        in MTProto too, but they may receive widget originals. An entity
        lookup that fails is not a refusal — the server enforces the rule.
        With ``require_dm`` (the send paths), any peer that is not a DM is
        refused: widgets go to DMs only, and every other chat gets the
        same content as a rich message instead.
        """
        from .widget_messages import is_broadcast_channel, is_dm_peer

        peer = await self._input_peer_for(chat_id)
        if type(peer).__name__ == "InputPeerChannel":
            try:
                entity = await self._client.get_entity(peer)
            except Exception:
                logger.debug("Telegram widget peer entity lookup failed; proceeding",
                             exc_info=True)
                return peer, None
            if entity is not None and is_broadcast_channel(entity):
                return None, SendResult(
                    success=False,
                    error="Telegram widget refused: widgets cannot be sent to a channel "
                          "as an original; channels receive widgets only as forwards",
                )
        if require_dm and not is_dm_peer(peer):
            return None, SendResult(
                success=False,
                error="Telegram widget refused: widgets can only be sent inside a DM; "
                      "send the same content as a rich message instead",
            )
        return peer, None

    async def send_widget(self, chat_id: str, html: str, *, caption: str = "",
                          entities: Optional[Any] = None, width: int = 0, height: int = 0,
                          allow_queries: Optional[bool] = None,
                          shared_widget: Optional[bool] = None,
                          public_data: Optional[str] = None,
                          chat_data: Optional[str] = None,
                          reply_to: Optional[str] = None,
                          top_msg_id: Optional[int] = None,
                          reply_markup: Optional[Any] = None,
                          silent: Optional[bool] = None) -> SendResult:
        """Send a widget (layer-233 HTML message) to a DM.

        The HTML is validated locally first (raw and zlib sizes, state sizes);
        broadcast channels are refused — channels get forwards only — and
        any chat that is not a DM is refused: widgets go to DMs only, and
        every other chat gets the same content as a rich message instead.
        Returns the sent message id, with the ``widget_id`` in ``raw_response``.
        """
        if self._client is None:
            return SendResult(success=False, error="Telegram MTProto client is disconnected")
        from .widget_messages import (
            build_widget_media, send_widget_request, sent_widget_info,
        )
        try:
            media = build_widget_media(
                html, width=width, height=height, allow_queries=allow_queries,
                shared_widget=shared_widget, public_data=public_data, chat_data=chat_data,
            )
        except (TypeError, ValueError) as exc:
            return SendResult(success=False, error=f"Telegram widget refused: {exc}")
        try:
            peer, refusal = await self._widget_peer_or_refusal(chat_id, require_dm=True)
            if refusal is not None:
                return refusal
            request = send_widget_request(
                peer, media, message=caption, entities=entities, reply_to=reply_to,
                top_msg_id=top_msg_id, reply_markup=reply_markup, silent=silent,
            )
            result = await self._request_with_bounded_retry(request)
        except Exception as exc:
            logger.debug("Telegram MTProto widget send failed", exc_info=True)
            retry_after = getattr(exc, "seconds", None)
            return SendResult(
                success=False,
                error=f"Telegram MTProto widget send failed: {type(exc).__name__}",
                retry_after=retry_after if isinstance(retry_after, (int, float)) else None,
                retryable=bool(retry_after is not None),
            )
        message_id, widget_id = sent_widget_info(result)
        if message_id is None:
            message_id = self._sent_message_id(result, random_id=request.random_id, peer=peer)
        return SendResult(success=True, message_id=message_id,
                          raw_response={"widget_id": widget_id} if widget_id is not None else None)

    async def send_widget_by_id(self, chat_id: str, widget_id: int, *, caption: str = "",
                                chat_data: Optional[str] = None,
                                reply_to: Optional[str] = None,
                                top_msg_id: Optional[int] = None,
                                reply_markup: Optional[Any] = None,
                                silent: Optional[bool] = None) -> SendResult:
        """Send an existing widget again by its id: a new message, same shared object.

        The same DM-only rule as ``send_widget`` applies: a group or channel
        peer is refused. Returns the sent message id, with the ``widget_id``
        in ``raw_response``.
        """
        if self._client is None:
            return SendResult(success=False, error="Telegram MTProto client is disconnected")
        from .widget_messages import (
            build_widget_media_by_id, send_widget_request, sent_widget_info,
        )
        try:
            media = build_widget_media_by_id(widget_id, chat_data=chat_data)
        except (TypeError, ValueError) as exc:
            return SendResult(success=False, error=f"Telegram widget refused: {exc}")
        try:
            peer, refusal = await self._widget_peer_or_refusal(chat_id, require_dm=True)
            if refusal is not None:
                return refusal
            request = send_widget_request(
                peer, media, message=caption, reply_to=reply_to,
                top_msg_id=top_msg_id, reply_markup=reply_markup, silent=silent,
            )
            result = await self._request_with_bounded_retry(request)
        except Exception as exc:
            logger.debug("Telegram MTProto widget send failed", exc_info=True)
            retry_after = getattr(exc, "seconds", None)
            return SendResult(
                success=False,
                error=f"Telegram MTProto widget send failed: {type(exc).__name__}",
                retry_after=retry_after if isinstance(retry_after, (int, float)) else None,
                retryable=bool(retry_after is not None),
            )
        message_id, sent_widget_id = sent_widget_info(result)
        if message_id is None:
            message_id = self._sent_message_id(result, random_id=request.random_id, peer=peer)
        return SendResult(success=True, message_id=message_id,
                          raw_response={"widget_id": sent_widget_id} if sent_widget_id is not None else None)

    async def upload_widget(self, chat_id: str, html: str, *, width: int = 0, height: int = 0,
                            allow_queries: Optional[bool] = None,
                            shared_widget: Optional[bool] = None,
                            public_data: Optional[str] = None,
                            chat_data: Optional[str] = None) -> SendResult:
        """Upload a widget without sending it; the ``widget_id`` is in ``raw_response``."""
        if self._client is None:
            return SendResult(success=False, error="Telegram MTProto client is disconnected")
        from .widget_messages import build_widget_media, upload_widget_request
        try:
            media = build_widget_media(
                html, width=width, height=height, allow_queries=allow_queries,
                shared_widget=shared_widget, public_data=public_data, chat_data=chat_data,
            )
        except (TypeError, ValueError) as exc:
            return SendResult(success=False, error=f"Telegram widget refused: {exc}")
        try:
            peer, refusal = await self._widget_peer_or_refusal(chat_id)
            if refusal is not None:
                return refusal
            uploaded = await self._request_with_bounded_retry(upload_widget_request(peer, media))
        except Exception as exc:
            logger.debug("Telegram MTProto widget upload failed", exc_info=True)
            retry_after = getattr(exc, "seconds", None)
            return SendResult(
                success=False,
                error=f"Telegram MTProto widget upload failed: {type(exc).__name__}",
                retry_after=retry_after if isinstance(retry_after, (int, float)) else None,
                retryable=bool(retry_after is not None),
            )
        widget_id = getattr(uploaded, "widget_id", None)
        return SendResult(success=True,
                          raw_response={"widget_id": widget_id} if widget_id is not None else None)

    async def answer_widget_query(self, query_id: int, result: Optional[str] = None) -> bool:
        """Answer a widget tap. ``result=None`` is the empty answer.

        Always answer, even when the tap cannot be served — a tap
        unanswered for 15 s times out on the widget. Returns False only
        when the client is down or the answer itself fails.
        """
        if self._client is None:
            return False
        from .widget_messages import answer_widget_query_request
        try:
            request = answer_widget_query_request(query_id, result)
            await self._request_with_bounded_retry(request)
        except Exception:
            logger.debug("Telegram widget query answer failed", exc_info=True)
            return False
        return True

    async def edit_widget_message_data(self, chat_id: str, message_id: str, *,
                                       public_data: Optional[str] = None,
                                       chat_data: Optional[str] = None) -> SendResult:
        """Edit the state of an original widget message (sending bot only)."""
        if self._client is None:
            return SendResult(success=False, error="Telegram MTProto client is disconnected")
        from .widget_messages import edit_message_widget_data_request
        try:
            request = edit_message_widget_data_request(
                await self._input_peer_for(chat_id), message_id,
                public_data=public_data, chat_data=chat_data,
            )
            await self._request_with_bounded_retry(request)
        except (TypeError, ValueError) as exc:
            return SendResult(success=False, error=f"Telegram widget edit refused: {exc}")
        except Exception as exc:
            logger.debug("Telegram MTProto widget edit failed", exc_info=True)
            return SendResult(success=False,
                              error=f"Telegram MTProto widget edit failed: {type(exc).__name__}")
        return SendResult(success=True)

    async def edit_shared_widget_data(self, widget_id: int, public_data: str, *,
                                      cache_time: Optional[int] = None) -> SendResult:
        """Edit a shared widget object by its ``widget_id``.

        Works even when every carrier message is deleted or unknown to the
        bot. At most one edit per second per object — callers serialise
        through the tap loop's per-widget lock.
        """
        if self._client is None:
            return SendResult(success=False, error="Telegram MTProto client is disconnected")
        from .widget_messages import edit_shared_widget_data_request
        try:
            request = edit_shared_widget_data_request(widget_id, public_data, cache_time=cache_time)
            await self._request_with_bounded_retry(request)
        except (TypeError, ValueError) as exc:
            return SendResult(success=False, error=f"Telegram widget edit refused: {exc}")
        except Exception as exc:
            logger.debug("Telegram MTProto widget edit failed", exc_info=True)
            return SendResult(success=False,
                              error=f"Telegram MTProto widget edit failed: {type(exc).__name__}")
        return SendResult(success=True)

    async def edit_inline_widget_data(self, inline_message_id: Any, *,
                                      public_data: Optional[str] = None,
                                      chat_data: Optional[str] = None) -> SendResult:
        """Edit the state of a via-bot widget message by its inline id."""
        if self._client is None:
            return SendResult(success=False, error="Telegram MTProto client is disconnected")
        from .widget_messages import edit_inline_widget_data_request
        try:
            request = edit_inline_widget_data_request(
                inline_message_id, public_data=public_data, chat_data=chat_data,
            )
            await self._request_with_bounded_retry(request)
        except (TypeError, ValueError) as exc:
            return SendResult(success=False, error=f"Telegram widget edit refused: {exc}")
        except Exception as exc:
            logger.debug("Telegram MTProto widget edit failed", exc_info=True)
            return SendResult(success=False,
                              error=f"Telegram MTProto widget edit failed: {type(exc).__name__}")
        return SendResult(success=True)

    async def get_widget_public_data(self, chat_id: str, message_id: str, *,
                                     hash: int = 0) -> Any:
        """Read a shared widget's public state (``hash=0`` forces a full read).

        Returns the raw TL result (``WidgetPublicData`` or
        ``WidgetPublicDataNotModified``); ``None`` when the client is down.
        """
        if self._client is None:
            return None
        from .widget_messages import get_widget_public_data_request
        try:
            request = get_widget_public_data_request(
                await self._input_peer_for(chat_id), message_id, hash=hash,
            )
            return await self._request_with_bounded_retry(request)
        except Exception:
            logger.debug("Telegram widget public-data read failed", exc_info=True)
            return None
