"""Contract tests for the credential-free isolated-worker inference boundary."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from gateway.isolated_worker import Binding, BoundaryDenied, Peer
from gateway.isolated_worker import inference as inference_module
from gateway.isolated_worker.inference import (
    InferenceBroker,
    IsolatedOpenAIClient,
    MAX_REQUEST_BYTES,
    OpenRouterFreeTransport,
)


class FakeTransport:
    def __init__(self, response=None):
        self.response = response or {
            "id": "response-id",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
        }
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait = False
        self.error = None

    async def complete(self, request):
        self.requests.append(copy.deepcopy(request))
        self.started.set()
        if self.wait:
            await self.release.wait()
        if self.error:
            raise self.error
        return self.response


def binding(identity: str, stamp: str | None = None) -> Binding:
    return Binding(identity, stamp or hashlib.sha256((identity + "-auth").encode()).hexdigest(),
                   "room-1", "group", "thread-2")


def peer(identity: str, worker: str = "a" * 32, stamp: str | None = None) -> Peer:
    return Peer(worker, binding(identity, stamp), 20001, "/hermes-workers/" + worker)


def request(**overrides):
    value = {"model": "openrouter/free", "messages": [{"role": "user", "content": "hello"}]}
    value.update(overrides)
    return value


async def authorized(binding_value):
    return True


def test_identity_a_to_b_to_a_capability_is_bound_to_exact_worker_and_binding():
    async def run():
        broker = InferenceBroker(FakeTransport(), authorized)
        a_first, b, a_second = peer("identity-a", "a" * 32), peer("identity-b", "b" * 32), peer("identity-a", "c" * 32)
        cap_a = await broker.issue(a_first)
        cap_b = await broker.issue(b)
        cap_a_again = await broker.issue(a_second)
        assert cap_a != cap_a_again
        assert (await broker.infer(cap_a, a_first, request()))["choices"][0]["message"]["content"] == "ok"
        for stolen_peer in (b, a_second):
            with pytest.raises(BoundaryDenied):
                await broker.infer(cap_a, stolen_peer, request())
        with pytest.raises(BoundaryDenied):
            await broker.infer(cap_b, a_first, request())
        assert (await broker.infer(cap_b, b, request()))["choices"][0]["message"]["content"] == "ok"
        assert (await broker.infer(cap_a_again, a_second, request()))["choices"][0]["message"]["content"] == "ok"
    asyncio.run(run())


def test_stale_authorization_generation_after_provider_await_discards_result():
    async def run():
        valid_stamp = hashlib.sha256(b"v1").hexdigest()
        current = {"stamp": valid_stamp}
        transport = FakeTransport()
        transport.wait = True

        async def check(candidate):
            return candidate.authorization_stamp == current["stamp"]

        broker = InferenceBroker(transport, check)
        subject = peer("identity-a", stamp=valid_stamp)
        cap = await broker.issue(subject)
        task = asyncio.create_task(broker.infer(cap, subject, request()))
        await transport.started.wait()
        current["stamp"] = "revoked-generation"
        transport.release.set()
        with pytest.raises(BoundaryDenied):
            await task
    asyncio.run(run())


def test_free_route_is_fixed_and_paid_routing_or_arbitrary_url_headers_are_rejected():
    async def run():
        transport = FakeTransport()
        broker = InferenceBroker(transport, authorized)
        subject = peer("identity-a")
        cap = await broker.issue(subject)
        await broker.infer(cap, subject, request())
        sent = transport.requests[0]
        assert sent["model"] == "openrouter/free"
        assert sent["provider"] == {"max_price": {"prompt": 0, "completion": 0},
                                     "allow_fallbacks": False}
        for extra in (
            {"provider": {"max_price": {"prompt": 0, "completion": 1}}},
            {"provider": {"order": ["expensive-provider"]}},
            {"models": ["paid/model"]},
            {"model": "paid/model"},
            {"url": "https://attacker.invalid/collect"},
            {"headers": {"Authorization": "Bearer stolen"}},
        ):
            with pytest.raises(BoundaryDenied):
                await broker.infer(cap, subject, request(**extra))
    asyncio.run(run())


def test_bounded_inputs_streaming_rejection_and_faithful_tool_messages():
    async def run():
        transport = FakeTransport()
        broker = InferenceBroker(transport, authorized)
        subject = peer("identity-a")
        cap = await broker.issue(subject)
        payload = request(
            max_tokens=64,
            tools=[{"type": "function", "function": {"name": "lookup", "description": "find",
                     "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}],
            messages=[
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {
                        "name": "lookup", "arguments": "{\"q\":\"x\"}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "name": "lookup", "content": "result"},
            ],
        )
        await broker.infer(cap, subject, payload)
        assert transport.requests[0]["messages"] == payload["messages"]
        assert transport.requests[0]["tools"] == payload["tools"]
        for invalid in (request(max_tokens=0), request(max_tokens=4097),
                        request(stream=True), request(messages=[{"role": "user", "content": "x" * 40000}]),
                        request(messages=[{"role": "user", "content": [{"type": "image_url"}]}]),
                        request(foo="bar")):
            with pytest.raises(BoundaryDenied):
                await broker.infer(cap, subject, invalid)
        too_big = request(messages=[{"role": "user", "content": "z" * MAX_REQUEST_BYTES}])
        with pytest.raises(BoundaryDenied):
            await broker.infer(cap, subject, too_big)
    asyncio.run(run())


def test_transport_failures_and_invalid_responses_are_generic_and_never_expose_secret():
    async def run():
        secret = "sk-or-secret-synthetic"
        transport = FakeTransport()
        broker = InferenceBroker(transport, authorized)
        subject = peer("identity-a")
        cap = await broker.issue(subject)
        transport.error = RuntimeError(f"provider exploded with {secret}")
        with pytest.raises(BoundaryDenied) as caught:
            await broker.infer(cap, subject, request())
        assert secret not in str(caught.value)
        transport.error = None
        transport.response = {"error": f"raw upstream secret {secret}"}
        with pytest.raises(BoundaryDenied) as caught:
            await broker.infer(cap, subject, request())
        assert secret not in str(caught.value)
        with pytest.raises(ValueError):
            OpenRouterFreeTransport("bad\r\nX-Injected: yes")
    asyncio.run(run())


def test_revocation_cancels_active_inference_and_revokes_grants():
    async def run():
        transport = FakeTransport()
        transport.wait = True
        broker = InferenceBroker(transport, authorized)
        subject = peer("identity-a")
        cap = await broker.issue(subject)
        task = asyncio.create_task(broker.infer(cap, subject, request()))
        await transport.started.wait()
        assert await broker.invalidate(peer=subject) == 1
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(BoundaryDenied):
            await broker.infer(cap, subject, request())
    asyncio.run(run())


def test_broker_enforces_inflight_and_per_identity_rate_limits():
    async def run():
        rate_broker = InferenceBroker(FakeTransport(), authorized, rate_limit=1)
        subject = peer("identity-a")
        cap = await rate_broker.issue(subject)
        await rate_broker.infer(cap, subject, request())
        with pytest.raises(BoundaryDenied):
            await rate_broker.infer(cap, subject, request())

        transport = FakeTransport()
        transport.wait = True
        inflight_broker = InferenceBroker(transport, authorized, max_inflight=1)
        inflight_cap = await inflight_broker.issue(subject)
        first = asyncio.create_task(inflight_broker.infer(inflight_cap, subject, request()))
        await transport.started.wait()
        with pytest.raises(BoundaryDenied):
            await inflight_broker.infer(inflight_cap, subject, request())
        transport.release.set()
        await first
    asyncio.run(run())


def test_worker_openai_facade_uses_only_rpc_capability_and_preserves_tool_calls():
    calls = []

    def rpc(capability, payload):
        calls.append((capability, payload))
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]}

    client = IsolatedOpenAIClient(rpc, "opaque-capability")
    result = client.chat.completions.create(
        model="openrouter/free", messages=[{"role": "user", "content": "hi"}]
    )
    assert calls[0][0] == "opaque-capability"
    assert result.choices[0].message.tool_calls[0].function.arguments == "{}"
    assert type(result.model_dump(warnings=False)["choices"][0]["message"]) is dict
    with pytest.raises(BoundaryDenied):
        client.chat.completions.create(model="openrouter/free", messages=[], stream=True)


def test_final_authorization_wait_cannot_return_a_grant_revoked_during_callback():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def gated_check(_binding):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await release.wait()
            return True

        subject = peer("identity-a")
        broker = InferenceBroker(FakeTransport(), gated_check)
        issuing = asyncio.create_task(broker.issue(subject))
        await entered.wait()
        assert await broker.invalidate(peer=subject) == 1
        release.set()
        with pytest.raises(BoundaryDenied):
            await issuing
    asyncio.run(run())


def test_final_inference_authorization_wait_rechecks_grant_after_revocation():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def gated_check(_binding):
            nonlocal calls
            calls += 1
            if calls == 4:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # Let the authorization callback complete after invalidate()
                    # has removed the grant and canceled this active request.
                    await release.wait()
            return True

        subject = peer("identity-a")
        broker = InferenceBroker(FakeTransport(), gated_check)
        cap = await broker.issue(subject)
        task = asyncio.create_task(broker.infer(cap, subject, request()))
        await entered.wait()
        assert await broker.invalidate(peer=subject) == 1
        release.set()
        with pytest.raises(BoundaryDenied):
            await task
    asyncio.run(run())


def test_payload_is_deeply_snapshotted_before_authorization_yields():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        transport = FakeTransport()

        async def gated_check(_binding):
            nonlocal calls
            calls += 1
            if calls == 3:
                entered.set()
                await release.wait()
            return True

        subject = peer("identity-a")
        broker = InferenceBroker(transport, gated_check)
        cap = await broker.issue(subject)
        payload = request(
            messages=[{"role": "user", "content": "before"}],
            tools=[{"type": "function", "function": {
                "name": "lookup", "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"}}}}}],
        )
        inference = asyncio.create_task(broker.infer(cap, subject, payload))
        await entered.wait()
        payload["messages"][0]["content"] = "after"
        payload["tools"][0]["function"]["parameters"]["properties"]["query"]["type"] = "integer"
        release.set()
        await inference
        sent = transport.requests[0]
        assert sent["messages"][0]["content"] == "before"
        assert sent["tools"][0]["function"]["parameters"]["properties"]["query"]["type"] == "string"
    asyncio.run(run())


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        pass


def test_http_transport_accepts_chunked_json_but_pins_url_and_headers(monkeypatch):
    body = b'{"choices":[]}'
    seen = {}

    def handler(request_value):
        seen["request"] = request_value
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8",
                     "transfer-encoding": "chunked"},
            stream=_ChunkStream([body[:5], body[5:]]),
        )

    real_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(handler)
    client_options = {}

    def make_client(**kwargs):
        client_options.update(kwargs)
        return real_client(transport=mock_transport, **kwargs)

    monkeypatch.setattr(inference_module.httpx, "AsyncClient", make_client)

    async def run():
        result = await OpenRouterFreeTransport("synthetic-key").complete({
            "model": "openrouter/free",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
            "provider": {"max_price": {"prompt": 0, "completion": 0},
                         "allow_fallbacks": False},
        })
        assert result == {"choices": []}

    asyncio.run(run())
    sent = seen["request"]
    assert str(sent.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer synthetic-key"
    assert sent.headers["accept-encoding"] == "identity"
    assert client_options["follow_redirects"] is False
    assert client_options["trust_env"] is False


@pytest.mark.parametrize(
    "status,headers,chunks",
    [
        (307, {"location": "https://attacker.invalid/"}, [b'{"choices":[]}']),
        (500, {"content-type": "application/json"}, [b'{"choices":[]}']),
        (200, {"content-type": "text/plain"}, [b'{"choices":[]}']),
        (200, {"content-type": "application/json", "content-encoding": "gzip"},
         [b'{"choices":[]}']),
        (200, {"content-type": "application/json"}, [b"x" * 20]),
    ],
)
def test_http_transport_rejects_redirects_errors_encoding_and_oversize(
    monkeypatch, status, headers, chunks
):
    secret = "synthetic-provider-secret"
    mock_transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, headers=headers, stream=_ChunkStream(chunks))
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        inference_module.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=mock_transport, **kwargs),
    )

    async def run():
        transport = OpenRouterFreeTransport(secret, max_response_bytes=16)
        with pytest.raises(BoundaryDenied) as caught:
            await transport.complete({
                "model": "openrouter/free",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 10,
                "provider": {"max_price": {"prompt": 0, "completion": 0},
                             "allow_fallbacks": False},
            })
        assert secret not in str(caught.value)

    asyncio.run(run())


def test_http_transport_never_exposes_http_client_exception_text(monkeypatch):
    secret = "synthetic-provider-secret"
    failure = RuntimeError(f"upstream failure {secret}")

    def handler(_request):
        raise failure

    mock_transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        inference_module.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=mock_transport, **kwargs),
    )

    async def run():
        transport = OpenRouterFreeTransport(secret)
        with pytest.raises(BoundaryDenied) as caught:
            await transport.complete({
                "model": "openrouter/free",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 10,
                "provider": {"max_price": {"prompt": 0, "completion": 0},
                             "allow_fallbacks": False},
            })
        assert secret not in str(caught.value)
        assert str(failure) not in str(caught.value)

    asyncio.run(run())
