"""Narrow, identity-bound inference RPC for isolated workers.

The broker accepts only a free OpenRouter route and never acts as a general HTTP
proxy. The API key belongs to the trusted transport instance; it is not exposed
in a grant, request, environment, or worker-side client.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import time
import httpx
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

from . import Binding, BoundaryDenied, Peer


MODEL = "openrouter/free"
_PROVIDER_POLICY = {
    "max_price": {"prompt": 0, "completion": 0},
    "allow_fallbacks": False,
}
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MESSAGES = 64
MAX_TOOLS = 32
MAX_TOKENS = 4096
MAX_INFLIGHT = 8
MAX_GRANTS = 4096
MAX_RATE_IDENTITIES = 4096
RATE_LIMIT = 20
RATE_WINDOW_SECONDS = 60.0
DEFAULT_GRANT_TTL_SECONDS = 300.0


class InferenceTransport(Protocol):
    async def complete(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


AuthorizationCheck = Callable[[Binding], bool | Awaitable[bool]]


def _deny() -> BoundaryDenied:
    # Do not attach provider/network/parser exception text to a worker-visible error.
    return BoundaryDenied("Inference request denied")


def _json_size(value: Any) -> int:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _deny() from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise _deny()
    return len(encoded)


def _bounded_json_tree(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [10000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 12:
        raise _deny()
    if value is None or type(value) in (str, bool, int):
        if isinstance(value, str) and len(value) > 65536:
            raise _deny()
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _deny()
        return
    if isinstance(value, list):
        if len(value) > 4096:
            raise _deny()
        for item in value:
            _bounded_json_tree(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        if len(value) > 1024:
            raise _deny()
        for key, item in value.items():
            if len(key) > 256:
                raise _deny()
            _bounded_json_tree(item, depth=depth + 1, budget=budget)
        return
    raise _deny()


def _text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and len(value) <= maximum


def _validate_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise _deny()
    normalized: list[dict[str, Any]] = []
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            raise _deny()
        role = message.get("role")
        if not isinstance(role, str):
            raise _deny()
        if role in {"system", "developer", "user"}:
            if set(message) - {"role", "content", "name"}:
                raise _deny()
            content = message.get("content")
            if not _text(content, 32768):
                # Multimodal payloads can create an arbitrary remote-fetch surface.
                raise _deny()
            if "name" in message and not _text(message["name"], 128):
                raise _deny()
        elif role == "assistant":
            if set(message) - {"role", "content", "name", "tool_calls"}:
                raise _deny()
            content = message.get("content")
            if content is not None and not _text(content, 32768):
                raise _deny()
            if "name" in message and not _text(message["name"], 128):
                raise _deny()
            if "tool_calls" in message:
                calls = message["tool_calls"]
                if not isinstance(calls, list) or not 1 <= len(calls) <= MAX_TOOLS:
                    raise _deny()
                for call in calls:
                    if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                            or not _text(call["id"], 256) or call["type"] != "function"):
                        raise _deny()
                    fn = call["function"]
                    if (not isinstance(fn, dict) or set(fn) != {"name", "arguments"}
                            or not _text(fn["name"], 128)
                            or not _text(fn["arguments"], 65536)):
                        raise _deny()
        elif role == "tool":
            if set(message) - {"role", "content", "tool_call_id", "name"}:
                raise _deny()
            if (not _text(message.get("content"), 32768)
                    or not _text(message.get("tool_call_id"), 256)):
                raise _deny()
            if "name" in message and not _text(message["name"], 128):
                raise _deny()
        else:
            raise _deny()
        total_chars += sum(len(item) for item in message.values() if isinstance(item, str))
        if total_chars > 192 * 1024:
            raise _deny()
        normalized.append(dict(message))
    return normalized


def _validate_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise _deny()
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) != {"type", "function"}
                or tool["type"] != "function" or not isinstance(tool["function"], dict)):
            raise _deny()
        fn = tool["function"]
        if set(fn) - {"name", "description", "parameters", "strict"}:
            raise _deny()
        if not _text(fn.get("name"), 128) or fn["name"] in names:
            raise _deny()
        names.add(fn["name"])
        if "description" in fn and not _text(fn["description"], 2048):
            raise _deny()
        if "strict" in fn and type(fn["strict"]) is not bool:
            raise _deny()
        if "parameters" in fn:
            schema = fn["parameters"]
            if not isinstance(schema, dict):
                raise _deny()
            _bounded_json_tree(schema)
        result.append({"type": "function", "function": dict(fn)})
    return result


def validate_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fresh, bounded OpenAI request with all routing fixed by the broker."""
    if not isinstance(payload, Mapping):
        raise _deny()
    try:
        # Freeze the full caller-owned tree before any authorization await. This
        # also ensures validated nested schemas cannot change while the RPC waits.
        source = dict(payload)
        _bounded_json_tree(source)
        snapshot_bytes = json.dumps(
            source, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(snapshot_bytes) > MAX_REQUEST_BYTES:
            raise _deny()
        payload = json.loads(snapshot_bytes)
    except BoundaryDenied:
        raise
    except Exception:
        raise _deny() from None
    allowed = {
        "model", "messages", "max_tokens", "temperature", "top_p", "stop",
        "tools", "tool_choice", "response_format", "seed", "frequency_penalty",
        "presence_penalty", "parallel_tool_calls",
    }
    if set(payload) - allowed:
        # This rejects URL, headers, provider/model routing and arbitrary extensions.
        raise _deny()
    if "model" in payload and payload["model"] != MODEL:
        raise _deny()
    if "messages" not in payload:
        raise _deny()
    request: dict[str, Any] = {"model": MODEL, "messages": _validate_messages(payload["messages"])}
    max_tokens = payload.get("max_tokens", 1024)
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
        raise _deny()
    request["max_tokens"] = max_tokens
    for key, lower, upper in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0),
                              ("frequency_penalty", -2.0, 2.0),
                              ("presence_penalty", -2.0, 2.0)):
        if key in payload:
            value = payload[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise _deny()
            request[key] = value
    if "stop" in payload:
        value = payload["stop"]
        if _text(value, 256):
            request["stop"] = value
        elif (isinstance(value, list) and len(value) <= 4
              and all(_text(item, 256) for item in value)):
            request["stop"] = list(value)
        else:
            raise _deny()
    if "tools" in payload:
        request["tools"] = _validate_tools(payload["tools"])
    if "tool_choice" in payload:
        choice = payload["tool_choice"]
        if choice in {"auto", "none", "required"} if isinstance(choice, str) else False:
            request["tool_choice"] = choice
        elif isinstance(choice, dict) and set(choice) == {"type", "function"}:
            fn = choice["function"]
            if (choice["type"] != "function" or not isinstance(fn, dict)
                    or set(fn) != {"name"} or not _text(fn["name"], 128)):
                raise _deny()
            request["tool_choice"] = {"type": "function", "function": {"name": fn["name"]}}
        else:
            raise _deny()
    if "response_format" in payload:
        fmt = payload["response_format"]
        if not isinstance(fmt, dict) or fmt.get("type") != "json_object" or set(fmt) != {"type"}:
            raise _deny()
        request["response_format"] = {"type": "json_object"}
    if "seed" in payload:
        seed = payload["seed"]
        if type(seed) is not int or not -(2**31) <= seed < 2**31:
            raise _deny()
        request["seed"] = seed
    if "parallel_tool_calls" in payload:
        if type(payload["parallel_tool_calls"]) is not bool:
            raise _deny()
        request["parallel_tool_calls"] = payload["parallel_tool_calls"]
    if _json_size(request) > MAX_REQUEST_BYTES:
        raise _deny()
    return request


@dataclass
class _Grant:
    peer: Peer
    expires_at: float


class InferenceBroker:
    """Broker grants are opaque labels bound to the full attested peer and binding."""

    def __init__(self, transport: InferenceTransport, is_authorized: AuthorizationCheck,
                 *, grant_ttl: float = DEFAULT_GRANT_TTL_SECONDS,
                 max_inflight: int = MAX_INFLIGHT, rate_limit: int = RATE_LIMIT,
                 rate_window: float = RATE_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if (not math.isfinite(grant_ttl) or grant_ttl <= 0 or type(max_inflight) is not int
                or max_inflight <= 0 or type(rate_limit) is not int or rate_limit <= 0
                or not math.isfinite(rate_window) or rate_window <= 0):
            raise ValueError("Invalid inference broker limits")
        self._transport = transport
        self._is_authorized = is_authorized
        self._grant_ttl = grant_ttl
        self._max_inflight = max_inflight
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._clock = clock
        self._lock = asyncio.Lock()
        self._grants: dict[str, _Grant] = {}
        self._inflight: dict[asyncio.Task[Any], tuple[str, Peer]] = {}
        self._rate: dict[str, deque[float]] = defaultdict(deque)

    async def _authorized(self, binding: Binding) -> bool:
        try:
            result = self._is_authorized(binding)
            if hasattr(result, "__await__"):
                result = await result
            return result is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def issue(self, peer: Peer) -> str:
        if not isinstance(peer, Peer) or not await self._authorized(peer.binding):
            raise _deny()
        now = self._clock()
        cap = secrets.token_urlsafe(32)
        async with self._lock:
            self._expire_locked(now)
            if len(self._grants) >= MAX_GRANTS:
                raise _deny()
            self._grants[cap] = _Grant(peer=peer, expires_at=now + self._grant_ttl)
        if not await self._authorized(peer.binding):
            async with self._lock:
                self._grants.pop(cap, None)
            raise _deny()
        async with self._lock:
            self._check_grant_locked(cap, peer)
        return cap

    async def infer(self, capability: str, peer: Peer, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(capability, str) or not isinstance(peer, Peer):
            raise _deny()
        request = validate_request(payload)
        now = self._clock()
        task = asyncio.current_task()
        if task is None:
            raise _deny()
        async with self._lock:
            self._expire_locked(now)
            grant = self._grants.get(capability)
            if grant is None or grant.peer != peer or grant.peer.binding != peer.binding:
                raise _deny()
            if len(self._inflight) >= self._max_inflight:
                raise _deny()
            if peer.binding.key not in self._rate and len(self._rate) >= MAX_RATE_IDENTITIES:
                # Discard only buckets whose complete window has elapsed.
                stale = [key for key, values in self._rate.items()
                         if not values or values[-1] <= now - self._rate_window]
                for key in stale:
                    self._rate.pop(key, None)
                if peer.binding.key not in self._rate and len(self._rate) >= MAX_RATE_IDENTITIES:
                    raise _deny()
            bucket = self._rate[peer.binding.key]
            while bucket and bucket[0] <= now - self._rate_window:
                bucket.popleft()
            if len(bucket) >= self._rate_limit:
                raise _deny()
            bucket.append(now)
            self._inflight[task] = (capability, peer)
        try:
            if not await self._authorized(peer.binding):
                raise _deny()
            async with self._lock:
                self._check_grant_locked(capability, peer)
            provider_request = dict(request)
            provider_request["provider"] = {
                "max_price": dict(_PROVIDER_POLICY["max_price"]),
                "allow_fallbacks": False,
            }
            try:
                response = await self._transport.complete(provider_request)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise _deny() from None
            async with self._lock:
                self._check_grant_locked(capability, peer)
            if not await self._authorized(peer.binding):
                raise _deny()
            async with self._lock:
                self._check_grant_locked(capability, peer)
            return self._validate_response(response)
        finally:
            async with self._lock:
                self._inflight.pop(task, None)

    def _check_grant_locked(self, capability: str, peer: Peer) -> None:
        grant = self._grants.get(capability)
        if (grant is None or grant.expires_at <= self._clock()
                or grant.peer != peer or grant.peer.binding != peer.binding):
            raise _deny()

    def _expire_locked(self, now: float) -> None:
        expired = {cap for cap, grant in self._grants.items() if grant.expires_at <= now}
        for cap in expired:
            self._grants.pop(cap, None)

    @staticmethod
    def _validate_response(response: Any) -> Mapping[str, Any]:
        try:
            if not isinstance(response, Mapping):
                raise ValueError
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise ValueError
            parsed = json.loads(encoded)
            if (not isinstance(parsed, dict) or not isinstance(parsed.get("choices"), list)
                    or len(parsed["choices"]) > MAX_MESSAGES):
                raise ValueError
            for choice in parsed["choices"]:
                if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                    raise ValueError
            return parsed
        except Exception:
            raise _deny() from None

    async def invalidate(self, *, peer: Peer | None = None,
                         binding: Binding | None = None) -> int:
        """Revoke grants and cancel active calls for the selected peer/binding."""
        if (peer is None) == (binding is None):
            raise ValueError("Specify exactly one of peer or binding")
        async with self._lock:
            def matches(candidate: Peer) -> bool:
                return candidate == peer if peer is not None else candidate.binding == binding

            revoked = [cap for cap, grant in self._grants.items() if matches(grant.peer)]
            for cap in revoked:
                self._grants.pop(cap, None)
            tasks = [task for task, (_, active_peer) in self._inflight.items()
                     if matches(active_peer)]
            current = asyncio.current_task()
            for task in tasks:
                if task is not current:
                    task.cancel()
            return len(revoked)


class OpenRouterFreeTransport:
    """Bounded HTTP client pinned to OpenRouter's chat-completion endpoint."""

    _url = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, *, timeout: float = 45.0,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        if (not isinstance(api_key, str) or not api_key or len(api_key) > 8192
                or not re.fullmatch(r"[\x21-\x7e]+", api_key)
                or not math.isfinite(timeout) or timeout <= 0
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES):
            raise ValueError("Invalid OpenRouter transport configuration")
        self.__api_key = api_key
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    async def complete(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(request, Mapping):
            raise _deny()
        provider = request.get("provider")
        if (not isinstance(provider, dict)
                or set(provider) != {"max_price", "allow_fallbacks"}
                or provider.get("allow_fallbacks") is not False
                or not isinstance(provider.get("max_price"), dict)
                or set(provider["max_price"]) != {"prompt", "completion"}
                or any(type(value) not in (int, float) or value != 0
                       for value in provider["max_price"].values())
                or request.get("model") != MODEL):
            raise _deny()
        fields = {key: value for key, value in request.items() if key != "provider"}
        if validate_request(fields) != fields:
            raise _deny()
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise _deny()
        try:
            async with asyncio.timeout(self._timeout):
                headers = {
                    "Authorization": f"Bearer {self.__api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                }
                timeout = httpx.Timeout(self._timeout)
                async with httpx.AsyncClient(
                    follow_redirects=False, trust_env=False, timeout=timeout
                ) as client:
                    async with client.stream(
                        "POST", self._url, headers=headers, content=body
                    ) as response:
                        if response.status_code != 200:
                            raise _deny()
                        content_type = response.headers.get("content-type", "")
                        if content_type.split(";", 1)[0].strip().lower() != "application/json":
                            raise _deny()
                        encoding = response.headers.get("content-encoding", "identity").strip().lower()
                        if encoding not in ("", "identity"):
                            raise _deny()
                        data = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(data) + len(chunk) > self._max_response_bytes:
                                raise _deny()
                            data.extend(chunk)
                        if not data:
                            raise _deny()
                        parsed = json.loads(data)
                        if not isinstance(parsed, dict):
                            raise _deny()
                        return parsed
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _deny() from None


class _ResponseObject(dict):
    """Small attribute-access wrapper matching common OpenAI SDK response usage."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        """Match the OpenAI response method consumed by Hermes' model helpers."""
        return {key: self._dump_value(value) for key, value in self.items()}

    @classmethod
    def _dump_value(cls, value: Any) -> Any:
        if isinstance(value, _ResponseObject):
            return {key: cls._dump_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._dump_value(item) for item in value]
        return value

    @classmethod
    def wrap(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return cls({key: cls.wrap(item) for key, item in value.items()})
        if isinstance(value, list):
            return [cls.wrap(item) for item in value]
        return value


class _ChatCompletions:
    def __init__(self, rpc: Callable[[str, Mapping[str, Any]], Any], capability: str,
                 defaults: Mapping[str, Any]):
        self._rpc = rpc
        self._capability = capability
        self._defaults = dict(defaults)

    def create(self, **kwargs: Any) -> Any:
        options = {**self._defaults, **kwargs}
        if options.get("stream") is True:
            raise _deny()
        options.pop("stream", None)
        response = self._rpc(self._capability, options)
        return _ResponseObject.wrap(response)


class _Chat:
    def __init__(self, rpc: Callable[[str, Mapping[str, Any]], Any], capability: str,
                 defaults: Mapping[str, Any]):
        self.completions = _ChatCompletions(rpc, capability, defaults)


class IsolatedOpenAIClient:
    """Worker-side OpenAI-shaped facade over an authenticated inference RPC.

    ``rpc`` is expected to be the worker's private, supervisor-attested channel
    adapter. It receives only the opaque grant and request; no peer identity fields
    are accepted from the model-facing client.
    """

    def __init__(self, rpc: Callable[[str, Mapping[str, Any]], Any], capability: str,
                 defaults: Mapping[str, Any] | None = None):
        if not callable(rpc) or not isinstance(capability, str) or not capability:
            raise ValueError("Invalid inference RPC client configuration")
        self._rpc = rpc
        self._capability = capability
        self._defaults = dict(defaults or {})
        self.chat = _Chat(rpc, capability, self._defaults)

    def with_options(self, **options: Any) -> IsolatedOpenAIClient:
        """Match the OpenAI client's immutable per-call option pattern."""
        return IsolatedOpenAIClient(self._rpc, self._capability,
                                    {**self._defaults, **options})

    def close(self) -> None:
        """The authenticated RPC channel is owned by the supervisor, not this facade."""


def create_openai_client(rpc: Callable[[str, Mapping[str, Any]], Any],
                         capability: str) -> IsolatedOpenAIClient:
    """Factory for worker integrations that replace AIAgent's provider client."""
    return IsolatedOpenAIClient(rpc, capability)
