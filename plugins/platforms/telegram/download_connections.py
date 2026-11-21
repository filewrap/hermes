"""Transfer-scoped independent MTProto senders for parallel Telegram downloads.

Targets Telethon 1.45.0's private sender/DC/initialization APIs, not a public
multi-connection extension point. Other clients/versions retain the iterator's
shared-sender path. No session file, login or custom export/import is involved.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator

from telethon.client.downloads import DownloadMethods
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl import functions

logger = logging.getLogger(__name__)
# Shared across one adapter's concurrent downloads, never multiplied per file.
# Owner-requested five-way trial between the four-way baseline and slower six.
# A throughput gain is unproven until the owner repeats the same live transfer.
MAX_DOWNLOAD_CONNECTIONS = 5


async def _complete_cleanup(awaitable: Any) -> None:
    """Finish finite resource teardown before propagating even repeated cancellation."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


@dataclass
class _SenderSlot:
    """One bounded pool slot; its sender is idle while the slot is queued."""

    sender: Any = None
    client: Any = None
    dc_id: int | None = None
    auth_key: bytes | None = None

    def clear(self) -> Any:
        sender = self.sender
        self.sender = self.client = self.dc_id = self.auth_key = None
        return sender


class DownloadMethodsFacade(DownloadMethods):
    """Native download iterator routed through one download-only sender."""

    def __init__(self, client: Any, sender: Any, dc_id: int):
        self._main_client = client
        self._sender = sender
        # The native iterator sees this sender as already attached to the file DC.
        self.session = SimpleNamespace(dc_id=dc_id)
        self._migration_borrows: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._main_client, name)

    async def _call(self, sender: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        if sender is self._sender:
            # Extra home-DC sessions must not become update consumers for the
            # gateway. Native borrowed senders keep their existing behavior.
            request = functions.InvokeWithoutUpdatesRequest(request)
        return await self._main_client._call(sender, request, *args, **kwargs)

    async def _borrow_exported_sender(self, dc_id: int) -> Any:
        sender = await self._main_client._borrow_exported_sender(dc_id)
        self._migration_borrows.append(sender)
        return sender

    async def _return_exported_sender(self, sender: Any) -> None:
        await self._main_client._return_exported_sender(sender)
        self._migration_borrows.remove(sender)

    async def close(self) -> None:
        # Telethon 1.45 replaces the current sender on successive migrations,
        # returning only the last at iterator.close(). Balance any overwritten
        # borrows as well, including repeated borrows of the same sender.
        while self._migration_borrows:
            await self._return_exported_sender(self._migration_borrows[-1])


class DownloadConnectionPool:
    """At most five dedicated senders shared by concurrent adapter operations.

    All part tasks must be joined before their transfer context exits. Idle
    senders close after the final transfer; no persistent cleanup task remains.
    """

    def __init__(self, capacity: int = MAX_DOWNLOAD_CONNECTIONS) -> None:
        if not 1 <= capacity <= MAX_DOWNLOAD_CONNECTIONS:
            raise ValueError(f"Download connections must be bounded between 1 and {MAX_DOWNLOAD_CONNECTIONS}")
        self.capacity = capacity
        self._slots: asyncio.Queue[_SenderSlot] = asyncio.Queue(maxsize=capacity)
        for _ in range(capacity):
            self._slots.put_nowait(_SenderSlot())
        self._lock = asyncio.Lock()
        self._active_transfers = 0
        self._active_leases = 0
        self._leases_released = asyncio.Event()
        self._leases_released.set()

    @staticmethod
    def supports(client: Any) -> bool:
        import telethon
        from telethon import TelegramClient
        return telethon.__version__ == "1.45.0" and type(client) is TelegramClient

    @asynccontextmanager
    async def transfer(self, client: Any) -> AsyncIterator[DownloadConnectionPool]:
        if not self.supports(client):
            raise TypeError("DownloadConnectionPool requires Telethon 1.45.0 TelegramClient")
        async with self._lock:
            self._active_transfers += 1
        try:
            yield self
        finally:
            await _complete_cleanup(self._release_transfer())

    async def _release_transfer(self) -> None:
        async with self._lock:
            self._active_transfers -= 1
            last_transfer = self._active_transfers == 0
        if not last_transfer:
            return
        await self._leases_released.wait()
        async with self._lock:
            # A new transfer can start while we wait for the final leases.
            if self._active_transfers:
                return
            for _ in range(self.capacity):
                slot = self._slots.get_nowait()
                try:
                    await self._disconnect(slot.clear())
                finally:
                    # Never strand capacity even if a disconnect is interrupted.
                    self._slots.put_nowait(slot)

    @asynccontextmanager
    async def connection(self, client: Any, dc_id: int) -> AsyncIterator[DownloadMethodsFacade]:
        if not self.supports(client):
            raise TypeError("DownloadConnectionPool requires Telethon 1.45.0 TelegramClient")
        async with self._lock:
            if self._active_transfers <= 0:
                raise RuntimeError("connection() must be used inside transfer()")
            self._active_leases += 1
            self._leases_released.clear()
        slot = None
        facade = None
        try:
            slot = await self._slots.get()
            auth_key = await self._authorization_key(client, dc_id)
            if (slot.sender is not None and
                    (slot.client is not client or slot.dc_id != dc_id or slot.auth_key != auth_key
                     or not slot.sender.is_connected())):
                await self._disconnect(slot.clear())
            if slot.sender is None:
                sender = await self._create_sender(client, dc_id, auth_key)
                slot.sender, slot.client, slot.dc_id, slot.auth_key = sender, client, dc_id, auth_key
            facade = DownloadMethodsFacade(client, slot.sender, dc_id)
            yield facade
        finally:
            await _complete_cleanup(self._release_connection(slot, facade))

    async def _release_connection(self, slot: _SenderSlot | None,
                                  facade: DownloadMethodsFacade | None) -> None:
        try:
            if facade is not None:
                await facade.close()
        finally:
            if slot is not None:
                self._slots.put_nowait(slot)
            async with self._lock:
                self._active_leases -= 1
                if self._active_leases == 0:
                    self._leases_released.set()

    @staticmethod
    async def _authorization_key(client: Any, dc_id: int) -> bytes:
        borrowed = None
        try:
            if client.session.dc_id == dc_id:
                source = client._sender
            else:
                borrowed = await client._borrow_exported_sender(dc_id)
                source = borrowed
            key = getattr(getattr(source, "auth_key", None), "key", None)
            if not key:
                raise RuntimeError(f"No Telegram authorization key available for DC {dc_id}")
            return memoryview(key).tobytes()
        finally:
            if borrowed is not None:
                await _complete_cleanup(client._return_exported_sender(borrowed))

    @staticmethod
    async def _create_sender(client: Any, dc_id: int, auth_key: bytes) -> Any:
        from telethon.crypto.authkey import AuthKey
        from telethon.tl.alltlobjects import LAYER

        sender = MTProtoSender(
            AuthKey(memoryview(auth_key).tobytes()), loggers=client._log,
            retries=client._connection_retries, delay=client._retry_delay,
            auto_reconnect=client._auto_reconnect, connect_timeout=client._timeout,
            # Suppress request-associated updates below, but never drop an
            # unsolicited update: use this same account's existing gateway queue.
            updates_queue=client._updates_queue,
        )
        try:
            dc = await client._get_dc(dc_id)
            await sender.connect(client._connection(
                dc.ip_address, dc.port, dc.id, loggers=client._log,
                proxy=client._proxy, local_addr=client._local_addr,
            ))
            init = copy.copy(client._init_request)
            init.query = functions.help.GetConfigRequest()
            # Match TelegramBaseClient.connect(receive_updates=False) exactly.
            request = functions.InvokeWithoutUpdatesRequest(init)
            await client._call(sender, functions.InvokeWithLayerRequest(LAYER, request))
            return sender
        except BaseException:
            await _complete_cleanup(DownloadConnectionPool._disconnect(sender))
            raise

    @staticmethod
    async def _disconnect(sender: Any) -> None:
        if sender is None:
            return
        try:
            await sender.disconnect()
        except Exception as exc:
            # Native disconnect stops its tasks; report close failures without
            # replacing an authentication/request error or exposing key material.
            logger.warning("Telegram download sender cleanup failed: %s", type(exc).__name__)
