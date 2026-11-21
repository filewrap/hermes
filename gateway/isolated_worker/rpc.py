"""Private inherited AF_UNIX inference channel for an external supervisor.

Each request carries kernel SCM_CREDENTIALS. The trusted supervisor must verify
PID/UID/GID against its live cgroup/worker registry; socketpair SO_PEERCRED alone
would describe its creator, not the eventual child. No listening endpoint, path,
provider key, caller-chosen identity, URL or arbitrary gateway callback exists.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import struct
import threading
from typing import Callable

from . import BoundaryDenied, Peer

MAX_PACKET = 128 * 1024
_CREDENTIALS = struct.Struct("3i")


def _encode(value: dict) -> bytes:
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > MAX_PACKET:
            raise ValueError
        return raw
    except (TypeError, ValueError, RecursionError):
        raise BoundaryDenied("Invalid inference RPC packet") from None


def _decode(raw: bytes) -> dict:
    try:
        if not raw or len(raw) > MAX_PACKET:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise BoundaryDenied("Invalid inference RPC packet") from None


def _validate_socket(channel: socket.socket):
    if (channel.family != socket.AF_UNIX
            or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET):
        raise BoundaryDenied("Private sequenced Unix channel required")


class InferenceRPCClient:
    """Worker-side callable accepted by inference.create_openai_client.

    The inherited descriptor must be private to this identity's worker boundary.
    Descendants may share it; sibling workers must never receive or mount it.
    """

    def __init__(self, channel: socket.socket, *, timeout: float = 60):
        _validate_socket(channel)
        if not 0 < timeout <= 300:
            raise BoundaryDenied("Invalid inference RPC timeout")
        self.channel = channel
        self.channel.settimeout(timeout)
        self._lock = threading.Lock()

    def close(self):
        self.channel.close()

    def __call__(self, capability: str, request: dict) -> dict:
        request_id = secrets.token_hex(16)
        packet = _encode({"version": 1, "request_id": request_id,
                          "capability": capability, "request": request})
        with self._lock:
            try:
                if self.channel.send(packet) != len(packet):
                    raise BoundaryDenied("Incomplete inference RPC packet")
                raw, _, flags, _ = self.channel.recvmsg(MAX_PACKET + 1)
                if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                    raise BoundaryDenied("Truncated inference RPC reply")
                reply = _decode(raw)
                if (set(reply) != {"version", "request_id", "result"}
                        or type(reply["version"]) is not int or reply["version"] != 1
                        or reply["request_id"] != request_id or not isinstance(reply["result"], dict)):
                    raise BoundaryDenied("Inference RPC denied")
                return reply["result"]
            except Exception:
                self.channel.close()
                raise BoundaryDenied("Inference RPC unavailable") from None


async def _readable(channel: socket.socket):
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    fd = channel.fileno()
    def wake():
        if not ready.done():
            ready.set_result(None)
    loop.add_reader(fd, wake)
    try:
        await ready
    finally:
        loop.remove_reader(fd)


async def serve_inference(channel: socket.socket, *, peer: Peer, broker,
                          attest_sender: Callable[[Peer, int, int, int], bool],
                          idle_timeout: float = 60):
    """Serve exactly one supervisor-registered worker channel, then close it.

    attest_sender is trusted supervisor code, not an RPC parameter. It must check
    live authorization, sender process membership and dedicated host UID. Missing
    or uncertain registry/kernel state denies. Teardown must also revoke grants
    and kill the worker cgroup; closing this channel alone is not containment.
    """
    _validate_socket(channel)
    if not callable(attest_sender) or not isinstance(peer, Peer) or not 0 < idle_timeout <= 300:
        raise BoundaryDenied("Missing inference channel attestation")
    channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    channel.setblocking(False)
    try:
        while True:
            async with asyncio.timeout(idle_timeout):
                await _readable(channel)
            raw, ancillary, flags, _ = channel.recvmsg(MAX_PACKET + 1, socket.CMSG_SPACE(_CREDENTIALS.size))
            if not raw:
                return
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise BoundaryDenied("Truncated inference RPC request")
            if (len(ancillary) != 1 or ancillary[0][:2] != (socket.SOL_SOCKET, socket.SCM_CREDENTIALS)
                    or len(ancillary[0][2]) != _CREDENTIALS.size):
                raise BoundaryDenied("Missing kernel sender credentials")
            pid, uid, gid = _CREDENTIALS.unpack(ancillary[0][2])
            if uid != peer.uid or attest_sender(peer, pid, uid, gid) is not True:
                raise BoundaryDenied("Unattested inference sender")
            request = _decode(raw)
            if (set(request) != {"version", "request_id", "capability", "request"}
                    or type(request["version"]) is not int or request["version"] != 1
                    or not isinstance(request["request_id"], str) or len(request["request_id"]) != 32
                    or not isinstance(request["capability"], str) or len(request["capability"]) > 128
                    or not isinstance(request["request"], dict)):
                raise BoundaryDenied("Invalid inference RPC operation")
            result = await broker.infer(request["capability"], peer, request["request"])
            if attest_sender(peer, pid, uid, gid) is not True:
                raise BoundaryDenied("Inference sender revoked")
            reply = _encode({"version": 1, "request_id": request["request_id"], "result": result})
            # Sequenced socket writes are atomic; no partial frame is retried.
            if channel.send(reply) != len(reply):
                raise BoundaryDenied("Incomplete inference RPC reply")
    except asyncio.CancelledError:
        raise
    except Exception:
        raise BoundaryDenied("Inference channel closed") from None
    finally:
        channel.close()
