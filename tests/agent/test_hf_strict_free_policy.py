"""Regression tests for opt-in strict OpenRouter free-only enforcement.

All transports here are synthetic; these tests never need credentials or make network calls.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import strict_free_policy as policy


FREE_PROVIDER = "openrouter"
FREE_MODEL = "openrouter/free"
FREE_URL = "https://openrouter.ai/api/v1"
ZERO_PRICE_CAP = {
    "prompt": 0,
    "completion": 0,
    "request": 0,
    "image": 0,
}


def _enable_strict(monkeypatch):
    monkeypatch.setenv(policy.STRICT_FREE_ONLY_ENV, "1")


def _assert_capped(request):
    assert request["model"] == FREE_MODEL
    assert request["extra_body"]["provider"] == {
        "allow_fallbacks": False,
        "max_price": ZERO_PRICE_CAP,
    }


def test_policy_is_inert_without_opt_in(monkeypatch):
    monkeypatch.delenv(policy.STRICT_FREE_ONLY_ENV, raising=False)
    original = {"model": "local/model", "extra_body": {"custom": True}}

    assert policy.enabled() is False
    policy.validate_route("local", "local/model", "http://localhost:8000/v1", "chat_completions")
    request = policy.enforce_request(
        original,
        provider="local",
        model="local/model",
        base_url="http://localhost:8000/v1",
    )
    assert request == original
    assert request is not original
    assert "extra_body" in request and request["extra_body"] is original["extra_body"]


def test_canonical_free_request_gets_all_zero_price_caps_and_no_fallback(monkeypatch):
    _enable_strict(monkeypatch)
    request = policy.enforce_request(
        {"messages": [{"role": "user", "content": "hello"}]},
        provider=FREE_PROVIDER,
        model=FREE_MODEL,
        base_url=FREE_URL,
        api_mode="chat_completions",
    )

    _assert_capped(request)
    assert request["extra_body"]["provider"]["max_price"] == ZERO_PRICE_CAP
    assert set(request["extra_body"]["provider"]["max_price"]) == {
        "prompt", "completion", "request", "image"
    }
    assert request["extra_body"]["provider"]["allow_fallbacks"] is False


@pytest.mark.parametrize(
    "provider,model,base_url,api_mode",
    [
        ("openai", FREE_MODEL, FREE_URL, "chat_completions"),
        (FREE_PROVIDER, "openai/gpt-4o", FREE_URL, "chat_completions"),
        (FREE_PROVIDER, FREE_MODEL, "https://openrouter.ai/api/v1/evil", "chat_completions"),
        (FREE_PROVIDER, FREE_MODEL, "http://openrouter.ai/api/v1", "chat_completions"),
        (FREE_PROVIDER, FREE_MODEL, "https://proxy.example/api/v1", "chat_completions"),
        (FREE_PROVIDER, FREE_MODEL, FREE_URL, "responses"),
    ],
)
def test_noncanonical_provider_model_endpoint_or_transport_is_rejected(
    monkeypatch, provider, model, base_url, api_mode
):
    _enable_strict(monkeypatch)
    with pytest.raises(policy.StrictFreeOnlyError):
        policy.validate_route(provider, model, base_url, api_mode)


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "paid/model"},
        {"models": [FREE_MODEL, "paid/model"]},
        {"fallback": True},
        {"fallbacks": ["paid/model"]},
        {"fallback_providers": ["paid-provider"]},
        {"route": "paid"},
        {"plugins": [{"id": "web"}]},
        {"web_search": {"enabled": True}},
        {"web_search_options": {"search_context_size": "high"}},
        {"provider": {"order": ["paid-provider"]}},
        {"extra_body": {"model": "paid/model"}},
        {"extra_body": {"models": ["paid/model"]}},
        {"extra_body": {"plugins": [{"id": "web"}]}},
        {"extra_query": {"preset": "@preset/paid"}},
        {"extra_body": {"unreviewed_future_router_control": True}},
        {"extra_body": {"provider": {"allow_fallbacks": True}}},
        {"extra_body": {"provider": {"max_price": {"completion": 0.01}}}},
    ],
)
def test_request_routing_override_or_price_weakening_is_rejected(monkeypatch, payload):
    _enable_strict(monkeypatch)
    with pytest.raises(policy.StrictFreeOnlyError):
        policy.enforce_request(
            payload,
            provider=FREE_PROVIDER,
            model=FREE_MODEL,
            base_url=FREE_URL,
        )


def test_safe_config_passes_read_only_and_restored_unsafe_fallback_is_rejected(monkeypatch):
    _enable_strict(monkeypatch)
    safe = {
        "model": {
            "provider": FREE_PROVIDER,
            "default": FREE_MODEL,
            "base_url": FREE_URL,
            "api_mode": "chat_completions",
            "fallback_providers": [],
        },
        "fallback_providers": [],
        "auxiliary": {
            "free_only": True,
            "openrouter_model": FREE_MODEL,
            "compression": {
                "provider": FREE_PROVIDER,
                "model": FREE_MODEL,
                "base_url": FREE_URL,
                "api_mode": "chat_completions",
            },
        },
    }
    before = {key: (dict(value) if isinstance(value, dict) else value) for key, value in safe.items()}
    assert policy.validate_config(safe) == ()
    policy.assert_config_safe(safe)
    assert safe == before

    restored_unsafe = {
        **safe,
        "model": {**safe["model"], "fallback_model": {"provider": "openai", "model": "paid/model"}},
    }
    unchanged = {**restored_unsafe, "model": dict(restored_unsafe["model"])}
    diagnostics = policy.validate_config(restored_unsafe)
    assert diagnostics
    assert any("fallback_model" in message for message in diagnostics)
    with pytest.raises(policy.StrictFreeOnlyError, match="Unsafe strict free-only config"):
        policy.assert_config_safe(restored_unsafe)
    assert restored_unsafe == unchanged


def test_main_client_builder_rejects_before_client_or_network_construction(monkeypatch):
    _enable_strict(monkeypatch)
    from agent import agent_init

    build_client = Mock(side_effect=AssertionError("client construction must not run"))
    monkeypatch.setattr(agent_init, "_init_openai_client", build_client)
    agent = SimpleNamespace(
        provider=FREE_PROVIDER,
        model=FREE_MODEL,
        base_url="https://proxy.example/v1",
        api_mode="chat_completions",
    )
    with pytest.raises(policy.StrictFreeOnlyError):
        agent_init._build_client(agent, "not-a-real-key", agent.base_url, None)
    build_client.assert_not_called()


def test_main_fallback_chain_is_denied_before_mutating_agent(monkeypatch):
    _enable_strict(monkeypatch)
    from agent import agent_init

    agent = SimpleNamespace()
    with pytest.raises(policy.StrictFreeOnlyError, match="Main-agent fallback"):
        agent_init._init_fallback_chain(
            agent, {"provider": "openai", "model": "paid/model"}
        )
    assert not hasattr(agent, "_fallback_chain")


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
def test_main_turn_outbound_guard_caps_free_request_on_both_transports(monkeypatch, stream):
    _enable_strict(monkeypatch)
    from agent import relay_llm, turn_api_call
    from hermes_cli import middleware

    sent = []

    def passthrough_middleware(request, callback, **_kwargs):
        return callback(request)

    monkeypatch.setattr(middleware, "run_llm_execution_middleware", passthrough_middleware)
    monkeypatch.setattr(relay_llm, "execute", lambda request, callback, **_kwargs: callback(request))
    agent = SimpleNamespace(
        provider=FREE_PROVIDER,
        model=FREE_MODEL,
        base_url=FREE_URL,
        api_mode="chat_completions",
        client=object(),
        session_id="offline-test",
        platform="",
        is_subagent=False,
        _fallback_index=0,
        _has_stream_consumers=lambda: stream,
        _interruptible_streaming_api_call=lambda request, **_kwargs: sent.append(request) or "stream-response",
        _interruptible_api_call=lambda request: sent.append(request) or "response",
        _has_pending_redirect=lambda: False,
        clear_interrupt=lambda **_kwargs: False,
    )
    kwargs = {"messages": [{"role": "user", "content": "hello"}], "temperature": 0.2}
    verdict = turn_api_call.perform_api_call(
        agent,
        api_kwargs=kwargs,
        _original_api_kwargs=kwargs,
        _llm_middleware_trace=[],
        _moa_prepared_request=None,
        _retry=SimpleNamespace(),
        thinking_spinner=None,
        retry_count=0,
        api_call_count=1,
        api_request_id="request-1",
        effective_task_id="task-1",
        turn_id="turn-1",
        interrupted=False,
    )

    assert verdict.action == "fallthrough"
    assert len(sent) == 1
    _assert_capped(sent[0])


@pytest.mark.parametrize("transport", ["sync", "async", "stream"])
def test_auxiliary_boundaries_reject_paid_model_before_synthetic_sdk_dispatch(monkeypatch, transport):
    _enable_strict(monkeypatch)
    from agent import auxiliary_client as aux

    dispatched = Mock()
    client = SimpleNamespace(
        base_url=FREE_URL,
        chat=SimpleNamespace(completions=SimpleNamespace(create=dispatched)),
    )
    kwargs = {"model": "vendor/paid-model", "messages": []}
    with pytest.raises(policy.StrictFreeOnlyError):
        if transport == "sync":
            aux._relay_sync_completion(client, kwargs, provider=FREE_PROVIDER)
        elif transport == "async":
            asyncio.run(aux._relay_async_completion(client, kwargs, provider=FREE_PROVIDER))
        else:
            aux._relay_sync_stream(client, kwargs, provider=FREE_PROVIDER)
    dispatched.assert_not_called()


def test_auxiliary_sync_async_and_stream_send_free_caps_to_synthetic_client(monkeypatch):
    _enable_strict(monkeypatch)
    from agent import auxiliary_client as aux

    sync_sent = []
    sync_client = SimpleNamespace(base_url=FREE_URL)
    aux._relay_sync_completion(
        sync_client,
        {"model": FREE_MODEL, "messages": []},
        provider=FREE_PROVIDER,
        create=lambda request: sync_sent.append(request) or "sync-ok",
    )
    assert len(sync_sent) == 1
    _assert_capped(sync_sent[0])

    async_sent = []
    async def async_create(request):
        async_sent.append(request)
        return "async-ok"

    asyncio.run(
        aux._relay_async_completion(
            SimpleNamespace(base_url=FREE_URL),
            {"model": FREE_MODEL, "messages": []},
            provider=FREE_PROVIDER,
            create=async_create,
        )
    )
    assert len(async_sent) == 1
    _assert_capped(async_sent[0])

    stream_sent = []
    stream_client = SimpleNamespace(
        base_url=FREE_URL,
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **request: stream_sent.append(request) or "stream-ok"
        )),
    )
    aux._relay_sync_stream(
        stream_client, {"model": FREE_MODEL, "messages": []}, provider=FREE_PROVIDER
    )
    assert len(stream_sent) == 1
    _assert_capped(stream_sent[0])


@pytest.mark.parametrize("transport", ["sync", "async", "stream"])
def test_auxiliary_rechecks_after_relay_mutates_request_before_sdk(monkeypatch, transport):
    """The relay's final callback is a security boundary, not just the pre-relay check."""
    _enable_strict(monkeypatch)
    from agent import auxiliary_client as aux, relay_llm

    dispatched = Mock()
    client = SimpleNamespace(
        base_url=FREE_URL,
        chat=SimpleNamespace(completions=SimpleNamespace(create=dispatched)),
    )
    monkeypatch.setattr(aux, "_relay_auxiliary_metadata", lambda **_kwargs: ("openrouter", FREE_MODEL, {}))

    def mutate_then_call(request, callback, **_kwargs):
        request = dict(request)
        request["extra_body"] = {"models": ["vendor/paid-model"]}
        return callback(request)

    async def async_mutate_then_call(request, callback, **_kwargs):
        request = dict(request)
        request["model"] = "vendor/paid-model"
        return await callback(request)

    monkeypatch.setattr(relay_llm, "execute_current", mutate_then_call)
    monkeypatch.setattr(relay_llm, "execute_current_async", async_mutate_then_call)
    monkeypatch.setattr(relay_llm, "stream_current", lambda request, callback, **_kw: callback({
        **request, "extra_body": {"models": ["vendor/paid-model"]}
    }))
    kwargs = {"model": FREE_MODEL, "messages": []}

    with pytest.raises(policy.StrictFreeOnlyError):
        if transport == "sync":
            aux._relay_sync_completion(client, kwargs, provider=FREE_PROVIDER)
        elif transport == "async":
            asyncio.run(aux._relay_async_completion(client, kwargs, provider=FREE_PROVIDER))
        else:
            aux._relay_sync_stream(client, kwargs, provider=FREE_PROVIDER)
    dispatched.assert_not_called()


@pytest.mark.parametrize(
    "call",
    [
        lambda aux: aux._try_payment_fallback("openrouter", task="compression"),
        lambda aux: aux._try_discovery_chain(),
        lambda aux: aux._try_configured_fallback_chain("compression", "openrouter"),
        lambda aux: aux._try_main_fallback_chain("compression", "openrouter"),
    ],
    ids=["discovery", "direct-discovery", "configured-fallback", "main-fallback"],
)
def test_auxiliary_discovery_and_fallback_paths_are_denied(monkeypatch, call):
    _enable_strict(monkeypatch)
    from agent import auxiliary_client as aux

    with pytest.raises(policy.StrictFreeOnlyError):
        call(aux)


def test_auxiliary_auto_route_rejects_restored_paid_main_route_before_resolution(monkeypatch):
    _enable_strict(monkeypatch)
    from agent import auxiliary_client as aux

    resolver = Mock(side_effect=AssertionError("route resolver must not run"))
    monkeypatch.setattr(aux, "_try_main_provider_route", resolver)
    with pytest.raises(policy.StrictFreeOnlyError):
        aux._resolve_auto_route(
            {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
            task="compression",
        )
    resolver.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"preset": "paid-advisor-preset"},
        {"extra_body": {"preset": "paid-advisor-preset"}},
        {"tools": [{"type": "openrouter:advisor", "name": "advisor"}]},
        {"tools": [{"type": "openrouter:subagent", "name": "worker"}]},
        {"tools": [{"type": "web_search", "name": "search"}]},
        {"audio": {"voice": "alloy"}},
        {"modalities": ["text", "audio"]},
        {"modalities": ["image"]},
        {"messages": [{"role": "user", "content": [{"type": "file", "file_id": "file-1"}]}]},
        {"messages": [{"role": "user", "content": [{"type": "input_audio", "data": "..."}]}]},
    ],
)
def test_paid_presets_server_tools_and_nontext_payloads_are_denied(monkeypatch, payload):
    _enable_strict(monkeypatch)
    request = {"messages": [{"role": "user", "content": "hello"}], **payload}
    with pytest.raises(policy.StrictFreeOnlyError):
        policy.enforce_request(
            request, provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL
        )


def test_fully_assembled_main_and_aux_text_requests_keep_allowed_defaults(monkeypatch):
    """The guard should preserve ordinary generation controls and local function tools."""
    _enable_strict(monkeypatch)
    body = {
        "reasoning": {"effort": "low"},
        "include_reasoning": False,
        "top_k": 40,
        "min_p": 0.05,
        "repetition_penalty": 1.05,
        "usage": {"include": True},
    }
    assembled = {
        "messages": [{"role": "user", "content": "Summarize this text."}],
        "tools": [{"type": "function", "function": {"name": "local_tool", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 512,
        "modalities": ["text"],
        "extra_body": body,
    }
    agent = SimpleNamespace(
        provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL, api_mode="chat_completions"
    )

    main_request = policy.enforce_agent_request(agent, assembled)
    aux_request = policy.enforce_request(
        assembled, provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL
    )
    for actual in (main_request, aux_request):
        _assert_capped(actual)
        assert actual["tools"] == assembled["tools"]
        assert actual["modalities"] == ["text"]
        for key, value in body.items():
            assert actual["extra_body"][key] == value


def test_actual_request_client_endpoint_is_checked_before_sdk_dispatch(monkeypatch):
    _enable_strict(monkeypatch)
    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request

    sdk_create = Mock(side_effect=AssertionError("SDK dispatch must not run"))
    request_client = SimpleNamespace(
        base_url="https://proxy.example/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=sdk_create)),
    )
    agent = SimpleNamespace(
        provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL,
        api_mode="chat_completions", client=request_client,
    )
    with pytest.raises(policy.StrictFreeOnlyError):
        _dispatch_nonstreaming_api_request(
            agent,
            {"model": FREE_MODEL, "messages": []},
            make_client=lambda *_args, **_kwargs: request_client,
        )
    sdk_create.assert_not_called()


def test_managed_summary_rechecks_request_after_relay_mutation(monkeypatch):
    _enable_strict(monkeypatch)
    from agent import chat_completion_helpers as helpers, relay_llm

    sent = Mock()
    agent = SimpleNamespace(
        provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL,
        api_mode="chat_completions",
    )

    def mutate_then_call(request, callback, **_kwargs):
        return callback({**request, "extra_body": {"preset": "paid-preset"}})

    monkeypatch.setattr(relay_llm, "execute_current", mutate_then_call)
    with pytest.raises(policy.StrictFreeOnlyError):
        helpers._managed_summary_call(
            agent,
            "summary-request",
            {"model": FREE_MODEL, "messages": []},
            sent,
            retry_count=0,
        )
    sent.assert_not_called()


def test_chat_summary_checks_actual_summary_client_endpoint_before_dispatch(monkeypatch):
    _enable_strict(monkeypatch)
    from agent import chat_completion_helpers as helpers, relay_llm

    sdk_create = Mock(side_effect=AssertionError("summary SDK dispatch must not run"))
    summary_client = SimpleNamespace(
        base_url="https://proxy.example/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=sdk_create)),
    )
    agent = SimpleNamespace(
        provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL,
        api_mode="chat_completions", tools=[], _force_ascii_payload=False,
        _build_api_kwargs=lambda _messages: {"model": FREE_MODEL, "messages": []},
        _ensure_primary_openai_client=lambda **_kwargs: summary_client,
        # Route through the real dispatch seam, not a bare Mock: the endpoint check lives in
        # _dispatch_nonstreaming_api_request, which validates the ACTUAL per-request client
        # (summary_client, base_url proxy.example) against the free endpoint. A bare Mock would
        # bypass that check and the summary would dispatch.
        _interruptible_api_call=lambda request: helpers._dispatch_nonstreaming_api_request(
            agent, request,
            make_client=lambda reason, **_kwargs: agent._ensure_primary_openai_client(reason=reason)),
    )
    monkeypatch.setattr(relay_llm, "execute_current", lambda request, callback, **_kwargs: callback(request))
    attempt = helpers._chat_summary_attempt(agent, [], "summary-request")
    with pytest.raises(policy.StrictFreeOnlyError):
        attempt(0)
    sdk_create.assert_not_called()


@pytest.mark.parametrize("transport", ["main", "auxiliary"])
def test_real_openrouter_builders_preserve_safe_session_and_title_controls(monkeypatch, tmp_path, transport):
    """Exercise provider projection, not a hand-written approximation of its defaults."""
    _enable_strict(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import auxiliary_client as aux
    from agent.title_generator import _TITLE_RESPONSE_FORMAT
    from agent.transports.chat_completions import ChatCompletionsTransport
    from providers import get_provider_profile

    messages = [{"role": "user", "content": "hello"}]
    if transport == "main":
        assembled = ChatCompletionsTransport().build_kwargs(
            FREE_MODEL, messages, provider_profile=get_provider_profile(FREE_PROVIDER),
            base_url=FREE_URL, session_id="free-only-session", supports_reasoning=True,
            reasoning_config={"enabled": False},
        )
        assert assembled["extra_body"]["session_id"]
        sent = []
        from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
        client = SimpleNamespace(
            base_url=FREE_URL,
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **request: sent.append(request))),
        )
        agent = SimpleNamespace(
            provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL,
            api_mode="chat_completions", client=client,
        )
        _dispatch_nonstreaming_api_request(agent, assembled, make_client=lambda *_a, **_kw: client)
    else:
        assembled = aux._build_call_kwargs(
            FREE_PROVIDER, FREE_MODEL, messages, base_url=FREE_URL, task="title_generation",
            extra_body={"response_format": _TITLE_RESPONSE_FORMAT},
            reasoning_config={"enabled": False},
        )
        assert assembled["extra_body"]["response_format"] == _TITLE_RESPONSE_FORMAT
        sent = []
        aux._relay_sync_completion(
            SimpleNamespace(base_url=FREE_URL), assembled, provider=FREE_PROVIDER,
            create=lambda request: sent.append(request),
        )
    assert len(sent) == 1
    _assert_capped(sent[0])
    for key, value in assembled["extra_body"].items():
        assert sent[0]["extra_body"][key] == value


def test_unknown_extra_controls_report_names_without_values_and_remain_fail_closed(monkeypatch):
    _enable_strict(monkeypatch)
    with pytest.raises(policy.StrictFreeOnlyError) as error:
        policy.enforce_request(
            {"extra_body": {"unreviewed_control": "private-value"}},
            provider=FREE_PROVIDER, model=FREE_MODEL, base_url=FREE_URL,
        )
    assert "unreviewed_control" in str(error.value)
    assert "private-value" not in str(error.value)
