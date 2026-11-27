"""Real local RPC tests with synthetic provider; no deployment worker is launched."""
import asyncio
import os
import socket
from dataclasses import replace

import pytest

from gateway.isolated_worker import Binding, BoundaryDenied, Peer
from gateway.isolated_worker.inference import InferenceBroker, create_openai_client
from gateway.isolated_worker.rpc import InferenceRPCClient, serve_inference


class Provider:
    def __init__(self):
        self.requests = []
    async def complete(self, request):
        self.requests.append(request)
        return {"id": "synthetic", "choices": [{"message": {"role": "assistant", "content": "own answer"}}]}


def peer(identity="11", worker="a"):
    # NOTE: do NOT fake a non-zero uid here. Peer.__post_init__ requires
    # uid > 0, but serve_inference() compares peer.uid against the kernel
    # SO_PEERCRED uid of the sending socket, so a faked uid is rejected at
    # the channel (verified: peer uid 1000 + real uid 0 -> BoundaryDenied).
    # As root the two can never agree; these tests are skipped there (see the
    # skipif on the peer-dependent tests) and run as a non-root user in CI.
    return Peer(worker * 32, Binding(identity, "a" * 64, identity, "dm"), os.getuid(),
                "/hermes-workers/" + worker * 32)


_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_SKIP_AS_ROOT = pytest.mark.skipif(
    _RUNNING_AS_ROOT,
    reason="root (uid 0) can never satisfy both Peer (uid > 0) and the kernel "
    "SO_PEERCRED check in serve_inference; runs as non-root in CI",
)


def attestor(expected):
    def check(actual, pid, uid, gid):
        # Real kernel credentials, but synthetic registry/cgroup attestation.
        return actual == expected and (pid, uid, gid) == (os.getpid(), os.getuid(), os.getgid())
    return check


async def setup_channel(broker, p, attest=None):
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    task = asyncio.create_task(serve_inference(server, peer=p, broker=broker,
                                               attest_sender=attest or attestor(p), idle_timeout=2))
    await asyncio.sleep(0)
    return task, InferenceRPCClient(client, timeout=2)


@_SKIP_AS_ROOT
@pytest.mark.asyncio
async def test_real_rpc_a_b_a_and_free_only_provider_policy():
    provider = Provider()
    broker = InferenceBroker(provider, lambda _: True)
    a, b = peer(), peer("22", "b")
    caps = {p.worker_id: await broker.issue(p) for p in (a, b)}
    for p in (a, b, a):
        task, rpc = await setup_channel(broker, p)
        client = create_openai_client(rpc, caps[p.worker_id])
        reply = await asyncio.to_thread(client.chat.completions.create,
                                         model="openrouter/free", messages=[{"role": "user", "content": "hello"}])
        assert reply.choices[0].message.content == "own answer"
        rpc.close()
        await task
    assert len(provider.requests) == 3
    for request in provider.requests:
        assert request["model"] == "openrouter/free"
        assert request["provider"] == {"max_price": {"prompt": 0, "completion": 0}, "allow_fallbacks": False}


@_SKIP_AS_ROOT
@pytest.mark.asyncio
async def test_stolen_capability_over_real_channel_denied_before_provider():
    provider = Provider(); broker = InferenceBroker(provider, lambda _: True)
    a, b = peer(), peer("22", "b")
    cap = await broker.issue(a)
    task, rpc = await setup_channel(broker, b)
    with pytest.raises(BoundaryDenied):
        await asyncio.to_thread(rpc, cap, {"messages": [{"role": "user", "content": "hello"}]})
    with pytest.raises(BoundaryDenied):
        await task
    assert not provider.requests


@_SKIP_AS_ROOT
@pytest.mark.asyncio
async def test_kernel_sender_must_match_supervisor_registry():
    provider = Provider(); broker = InferenceBroker(provider, lambda _: True)
    p = peer(); cap = await broker.issue(p)
    task, rpc = await setup_channel(broker, p, lambda *_: False)
    with pytest.raises(BoundaryDenied):
        await asyncio.to_thread(rpc, cap, {"messages": [{"role": "user", "content": "hello"}]})
    with pytest.raises(BoundaryDenied):
        await task
    assert not provider.requests


@_SKIP_AS_ROOT
@pytest.mark.asyncio
async def test_caller_cannot_smuggle_peer_claim_or_arbitrary_operation():
    provider = Provider(); broker = InferenceBroker(provider, lambda _: True)
    p = peer(); cap = await broker.issue(p)
    task, rpc = await setup_channel(broker, p)
    import json
    rpc.channel.send(json.dumps({"version": 1, "request_id": "a" * 32,
                                  "capability": cap, "request": {}, "peer": {"identity": "99"}}).encode())
    with pytest.raises(BoundaryDenied):
        await task
    rpc.close()
    assert not provider.requests


def test_stream_socket_is_not_an_attested_message_channel():
    server, client = socket.socketpair()
    with server, client:
        with pytest.raises(BoundaryDenied):
            InferenceRPCClient(client)
