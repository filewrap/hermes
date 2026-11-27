"""Shared per-chat scheduler for Telegram task-progress cards.

The scheduler owns card edits, not agent execution. A rate limit or UI failure is
contained to that card; queued state is retained while Telegram's FloodWait
window expires.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class _Card:
    key: str
    kind: str
    send_start: Callable[[str], Awaitable[Any]]
    edit: Callable[[str, str], Awaitable[Any]]
    start_text: str
    latest_text: str
    on_started: Optional[Callable[[str], Awaitable[None]]] = None
    message_id: Optional[str] = None
    rendered_text: Optional[str] = None
    retry_at: float = 0.0
    active: bool = True
    terminal: bool = False
    disabled: bool = False
    resent: bool = False
    start_future: Optional[asyncio.Future] = None


def minimum_edit_spacing(active_kinds: list[str] | tuple[str, ...]) -> float:
    """Minimum spacing between intermediate edits for the current chat load."""
    count = len(active_kinds)
    if count >= 4:
        return 60.0
    if count == 3:
        return 45.0
    if count == 2:
        return 25.0
    if count == 1 and active_kinds[0] == "deep_research":
        return 25.0
    return 10.0


class TelegramProgressScheduler:
    """Serialize one chat's card operations and coalesce each card to its latest state."""

    def __init__(self, adapter: Any, chat_id: str) -> None:
        self.adapter = adapter
        self.chat_id = str(chat_id)
        self._loop = asyncio.get_running_loop()
        self._cards: dict[str, _Card] = {}
        self._last_intermediate_edit = 0.0
        self._wake = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self._closed = False
        self._registry_key = (id(self._loop), id(adapter), self.chat_id)

    async def begin(
        self,
        key: str,
        kind: str,
        start_text: str,
        *,
        send_start: Callable[[str], Awaitable[Any]],
        edit: Callable[[str, str], Awaitable[Any]],
        on_started: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Optional[str]:
        """Register a card and return its ID when the first send succeeds.

        A FloodWait resolves this call with ``None`` immediately, while the
        scheduler keeps the initial card and latest state queued for retry.
        """
        if self._closed:
            return None
        existing = self._cards.get(key)
        if existing is not None:
            if existing.message_id:
                return existing.message_id
            if existing.start_future is not None:
                return await existing.start_future
            return None
        future = self._loop.create_future()
        card = _Card(
            key=str(key), kind=str(kind), send_start=send_start, edit=edit,
            start_text=str(start_text), latest_text=str(start_text),
            on_started=on_started, start_future=future,
        )
        self._cards[card.key] = card
        self._wake.set()
        self._ensure_worker()
        return await future

    def update(self, key: str, text: str, *, terminal: bool = False) -> None:
        """Replace pending state; duplicate rendered/pending text is discarded."""
        card = self._cards.get(str(key))
        if card is None or card.disabled:
            return
        text = str(text)
        if text == card.latest_text and bool(terminal) == card.terminal:
            return
        card.latest_text = text
        if terminal:
            card.terminal = True
            card.active = False
        self._wake.set()
        self._ensure_worker()

    def active_task_count(self) -> int:
        return sum(1 for card in self._cards.values() if card.active and not card.disabled)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = self._loop.create_task(self._run(), name=f"telegram-progress-{self.chat_id}")

    @staticmethod
    def _retry_after(result: Any) -> Optional[float]:
        value = getattr(result, "retry_after", None)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, seconds) if seconds > 0 else None

    def _next_due(self, card: _Card, now: float) -> Optional[float]:
        if card.disabled:
            return None
        if card.message_id is None:
            return max(now, card.retry_at)
        if card.latest_text == card.rendered_text:
            return None
        if card.terminal:
            return max(now, card.retry_at)
        active_kinds = [item.kind for item in self._cards.values() if item.active and not item.disabled]
        spacing = minimum_edit_spacing(active_kinds or [card.kind])
        return max(now, card.retry_at, self._last_intermediate_edit + spacing)

    async def _wait_until_changed_or_due(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _run(self) -> None:
        try:
            while self._cards:
                self._wake.clear()
                now = time.monotonic()
                candidates: list[tuple[float, int, _Card]] = []
                future_due: list[float] = []
                for card in self._cards.values():
                    due = self._next_due(card, now)
                    if due is None:
                        continue
                    if due <= now:
                        # Terminal updates take priority once eligible; all calls
                        # still execute serially through this worker.
                        candidates.append((due, 0 if card.terminal else 1, card))
                    else:
                        future_due.append(due)
                if not candidates:
                    if future_due:
                        await self._wait_until_changed_or_due(max(0.0, min(future_due) - time.monotonic()))
                        continue
                    # No card is pending. Remove settled cards and let the worker
                    # exit so a future task can create a fresh scheduler.
                    for key, card in list(self._cards.items()):
                        if card.disabled or (card.terminal and card.message_id and card.latest_text == card.rendered_text):
                            self._cards.pop(key, None)
                    if not self._cards:
                        break
                    await self._wait_until_changed_or_due(3600.0)
                    continue

                _, _, card = min(candidates, key=lambda item: (item[0], item[1], item[2].key))
                is_start = card.message_id is None
                content = (card.latest_text if is_start and card.terminal else card.start_text) if is_start else card.latest_text
                try:
                    result = await (card.send_start(content) if is_start else card.edit(str(card.message_id), content))
                except Exception as exc:
                    logger.debug("Telegram progress-card %s failed (%s)", "send" if is_start else "edit", type(exc).__name__)
                    result = None

                if getattr(result, "success", False):
                    message_id = getattr(result, "message_id", None)
                    if is_start:
                        if not message_id:
                            self._disable(card, "Telegram did not return a progress-card message ID")
                            continue
                        card.message_id = str(message_id)
                        card.rendered_text = content
                        card.retry_at = 0.0
                        # A start card is the baseline: no intermediate edit is
                        # allowed until the task-load interval elapses.
                        self._last_intermediate_edit = time.monotonic()
                        if card.start_future is not None and not card.start_future.done():
                            card.start_future.set_result(card.message_id)
                        if card.on_started is not None:
                            try:
                                await card.on_started(card.message_id)
                            except Exception:
                                logger.debug("Telegram progress-card start callback failed", exc_info=True)
                    else:
                        card.rendered_text = content
                        card.retry_at = 0.0
                        if not card.terminal:
                            self._last_intermediate_edit = time.monotonic()
                    if card.terminal and card.latest_text == card.rendered_text:
                        self._cards.pop(card.key, None)
                    continue

                retry_after = self._retry_after(result) if result is not None else None
                if retry_after is not None:
                    card.retry_at = time.monotonic() + retry_after
                    if card.start_future is not None and not card.start_future.done():
                        card.start_future.set_result(None)
                    # Leave the latest content untouched. This retry belongs to
                    # the card only; agent execution and other cards keep going.
                    logger.info("Telegram progress-card retry queued after FloodWait (%.1fs)", retry_after)
                    continue

                error = getattr(result, "error", None) if result is not None else None
                if not is_start and not card.resent:
                    # A refused edit is permanent for this message ID (a
                    # deleted card, an unverifiable topic anchor): re-send
                    # the latest text as a fresh card once instead of
                    # freezing the only source of truth for the whole run.
                    # A second failure still disables the card.
                    card.resent = True
                    card.message_id = None
                    card.rendered_text = None
                    card.start_text = card.latest_text
                    card.retry_at = 0.0
                    continue
                self._disable(card, str(error or "Telegram progress-card operation failed"))
        finally:
            # Resolve any waiter even if cancellation or a scheduler bug occurs.
            for card in self._cards.values():
                if card.start_future is not None and not card.start_future.done():
                    card.start_future.set_result(None)
            self._cards.clear()
            self._closed = True
            if _SCHEDULERS.get(self._registry_key) is self:
                _SCHEDULERS.pop(self._registry_key, None)

    def _disable(self, card: _Card, reason: str) -> None:
        card.disabled = True
        card.active = False
        if card.start_future is not None and not card.start_future.done():
            card.start_future.set_result(None)
        # A disabled card is user-visible (a frozen progress card), so its
        # reason belongs in the normal logs, not debug-only.
        logger.warning("Telegram progress card disabled for %s: %s", self.chat_id, reason[:180])
        self._cards.pop(card.key, None)


_SCHEDULERS: dict[tuple[int, int, str], TelegramProgressScheduler] = {}


def scheduler_for(adapter: Any, chat_id: str | int) -> TelegramProgressScheduler:
    """Return the shared scheduler for one adapter/chat/event-loop tuple."""
    loop = asyncio.get_running_loop()
    key = (id(loop), id(adapter), str(chat_id))
    scheduler = _SCHEDULERS.get(key)
    if scheduler is None or scheduler._closed:
        scheduler = TelegramProgressScheduler(adapter, str(chat_id))
        _SCHEDULERS[key] = scheduler
    return scheduler
