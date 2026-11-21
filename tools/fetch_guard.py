"""Guarded fetch: one SSRF-guarded fetch entry point for every Hermes-owned
HTTP fetch, mirroring the external agent's ``fetchWithSsrFGuard`` shape.

Per hop, in order: scheme check → per-URL policy verdict (``is_safe_url`` with
an ``SsrFPolicy``: blocklist/allowlist before DNS, private-IP classes, the
cloud-metadata floor always on) → the SSRF-safe httpx transport (connect-time
re-validation + dial-by-IP, closing DNS-rebinding TOCTOU). Redirects are
followed manually (max 3, the external agent's default) with the target re-validated
before the next hop, loop detection, and sensitive headers + body dropped on
cross-origin hops (301/302 POST and 303 force GET, as in the external agent's
``rewriteRedirectInit``).

``trusted_origin`` is the configured-local-origin bypass: the first hop may
target the operator's own provider ``base_url`` (a LAN/loopback relay) without
the private-address verdict, but the metadata floor still applies and every
redirect target is re-validated in full. It exists for callers that built the
URL from the operator's own config, never for provider-supplied URLs.

Callers that fetch through a third party (web_extract vendors, the browser)
use the verdict helpers (``is_url_allowed`` / ``assert_url_allowed``) on the
requested URL and on every redirect/final URL instead of fetching here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from tools.ssrf_policy import SsrFPolicy, resolve_policy_for_url
from tools.url_safety import (
    SSRFConnectionBlocked,
    async_is_safe_url,
    create_ssrf_safe_async_client,
    create_ssrf_safe_client,
    is_always_blocked_url,
    is_safe_url,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Headers never carried across origins (the external agent strips sensitive cross-origin
# headers on redirect; the body headers go with a dropped body).
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_BODY_HEADERS = frozenset({"content-length", "content-type", "content-encoding", "transfer-encoding"})


class SsrFBlockedError(SSRFConnectionBlocked):
    """A fetch-guard refusal (policy verdict or redirect violation)."""


@dataclass
class GuardedFetchResult:
    """A guarded fetch's final response. ``response`` is an httpx response
    whose body the caller reads; ``close()`` releases the client. For
    ``fetch_with_ssrf_guard`` the body is already read; for the streaming
    entry point it is live until ``close()``."""

    response: Any
    final_url: str
    _close: Any = None

    def close(self) -> None:
        if self._close is not None:
            self._close()


def is_url_allowed(url: str, *, policy: Optional[SsrFPolicy] = None) -> bool:
    """The guard's verdict for one URL (pre-flight + policy). No fetch happens."""
    return is_safe_url(url, policy=policy)


async def async_is_url_allowed(url: str, *, policy: Optional[SsrFPolicy] = None) -> bool:
    """Async :func:`is_url_allowed` (DNS off the event loop)."""
    return await async_is_safe_url(url, policy=policy)


def assert_url_allowed(url: str, *, policy: Optional[SsrFPolicy] = None) -> str:
    """The URL when the guard allows it, else raise :class:`SsrFBlockedError`."""
    if not is_safe_url(url, policy=policy):
        raise SsrFBlockedError(f"Blocked: URL failed the SSRF safety check: {url}")
    return url


def _strip_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


def _drop_body_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _BODY_HEADERS}


def _rewrite_redirect_request(
    *, method: str, headers: dict[str, str], body: Any, status: int, cross_origin: bool,
) -> tuple[str, dict[str, str], Any]:
    """The next hop's (method, headers, body) after a redirect, mirroring
    the external agent: 303 (and 301/302 on POST) force GET and drop the body; a
    cross-origin hop drops the body either way. Hermes is stricter than
    the external agent on credentials: caller auth headers never survive ANY redirect
    (same-origin targets included — a redirect target is a different
    application until proven otherwise, and provider auth must not leak to
    it); only non-sensitive headers carry over."""
    next_method, next_body, next_headers = method, body, dict(headers)
    if (status == 303 and method not in ("GET", "HEAD")) or (status in (301, 302) and method == "POST"):
        next_method, next_body = "GET", None
    if cross_origin and next_method not in ("GET", "HEAD"):
        next_body = None
    if next_body is None and body is not None:
        next_headers = _drop_body_headers(next_headers)
    next_headers = _strip_sensitive_headers(next_headers)
    return next_method, next_headers, next_body


def _check_hop(
    url: str, *, policy: Optional[SsrFPolicy], require_https: bool, trusted_hop: bool,
) -> SsrFPolicy | None:
    """Validate one hop before it is fetched; returns its resolved policy."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SsrFBlockedError(f"Invalid URL: {url}") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise SsrFBlockedError(f"Invalid URL: must be http or https: {url}")
    if require_https and parsed.scheme.lower() != "https":
        raise SsrFBlockedError(f"URL must use https: {url}")
    hop_policy = resolve_policy_for_url(url, policy)
    if trusted_hop:
        # Operator's own origin: private-class verdict off for this hop only;
        # the metadata floor still applies (is_safe_url enforces it first).
        hop_policy = replace(hop_policy or SsrFPolicy(), allow_private_network=True)
        if is_always_blocked_url(url):
            raise SsrFBlockedError(f"Blocked: URL targets an always-blocked address: {url}")
    if not is_safe_url(url, policy=hop_policy):
        raise SsrFBlockedError(f"Blocked: URL failed the SSRF safety check: {url}")
    return hop_policy


def fetch_with_ssrf_guard(
    url: str,
    *,
    policy: Optional[SsrFPolicy] = None,
    timeout: float = 30.0,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    require_https: bool = False,
    headers: Optional[dict[str, str]] = None,
    trusted_origin: bool = False,
    max_bytes: Optional[int] = None,
) -> GuardedFetchResult:
    """GET *url* under the SSRF guard, following redirects manually. The body
    is read (up to *max_bytes* when given) and the client closed before return,
    so the result is safe to use after this call."""
    result = _fetch_streaming(
        url, policy=policy, timeout=timeout, max_redirects=max_redirects,
        require_https=require_https, headers=headers, trusted_origin=trusted_origin,
        method="GET", body=None,
    )
    try:
        body = result.response.read()
        if max_bytes is not None and len(body) > max_bytes:
            raise SsrFBlockedError(f"Blocked: response exceeded max_bytes ({max_bytes})")
        return result
    finally:
        result.close()


def stream_with_ssrf_guard(
    url: str,
    *,
    policy: Optional[SsrFPolicy] = None,
    timeout: float = 30.0,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    require_https: bool = False,
    headers: Optional[dict[str, str]] = None,
    trusted_origin: bool = False,
    method: str = "GET",
    body: Any = None,
) -> GuardedFetchResult:
    """Like :func:`fetch_with_ssrf_guard` but the response body stays live for
    streaming reads; the caller MUST call ``result.close()`` (or use the result
    as a context manager via ``close()`` in a ``finally``)."""
    return _fetch_streaming(
        url, policy=policy, timeout=timeout, max_redirects=max_redirects,
        require_https=require_https, headers=headers, trusted_origin=trusted_origin,
        method=method, body=body,
    )


def _fetch_streaming(
    url: str, *, policy: Optional[SsrFPolicy], timeout: float, max_redirects: int,
    require_https: bool, headers: Optional[dict[str, str]], trusted_origin: bool,
    method: str, body: Any,
) -> GuardedFetchResult:
    current_url, current_method = url, method.upper()
    current_headers = dict(headers or {})
    current_body = body
    visited: set[str] = set()
    # One client per hop, built from THAT hop's resolved policy: an
    # allowed-origins promotion (or a trusted first hop) must reach the
    # connect-time check too, or the transport would refuse a hop the
    # pre-flight verdict just allowed.
    client = None
    try:
        for hop in range(max_redirects + 1):
            hop_policy = _check_hop(
                current_url, policy=policy, require_https=require_https,
                trusted_hop=trusted_origin and hop == 0,
            )
            visit_key = f"{current_method} {current_url}"
            if visit_key in visited:
                raise SsrFBlockedError(f"Redirect loop detected: {current_url}")
            visited.add(visit_key)
            if client is not None:
                client.close()
            client = create_ssrf_safe_client(timeout=timeout, follow_redirects=False, policy=hop_policy)
            stream = client.stream(
                current_method, current_url, headers=current_headers, content=current_body)
            response = stream.__enter__()
            try:
                if response.status_code not in _REDIRECT_STATUSES:
                    hop_client, hop_stream = client, stream
                    return GuardedFetchResult(
                        response=response, final_url=current_url,
                        _close=lambda: (hop_stream.__exit__(None, None, None), hop_client.close()),
                    )
                if hop == max_redirects:
                    raise SsrFBlockedError(f"Too many redirects (limit: {max_redirects})")
                location = response.headers.get("location")
                if not location:
                    raise SsrFBlockedError(f"Redirect missing location header ({response.status_code})")
                next_url = urljoin(current_url, location)
                next_parsed, current_parsed = urlparse(next_url), urlparse(current_url)
                cross_origin = (next_parsed.scheme, next_parsed.hostname, next_parsed.port) != (
                    current_parsed.scheme, current_parsed.hostname, current_parsed.port)
                current_method, current_headers, current_body = _rewrite_redirect_request(
                    method=current_method, headers=current_headers, body=current_body,
                    status=response.status_code, cross_origin=cross_origin,
                )
                current_url = next_url
            finally:
                if response.status_code in _REDIRECT_STATUSES:
                    stream.__exit__(None, None, None)
        raise SsrFBlockedError(f"Too many redirects (limit: {max_redirects})")
    except Exception:
        if client is not None:
            client.close()
        raise


async def async_fetch_with_ssrf_guard(
    url: str,
    *,
    policy: Optional[SsrFPolicy] = None,
    timeout: float = 30.0,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    require_https: bool = False,
    headers: Optional[dict[str, str]] = None,
    trusted_origin: bool = False,
    max_bytes: Optional[int] = None,
) -> GuardedFetchResult:
    """Async :func:`fetch_with_ssrf_guard` (body read, client closed)."""
    current_url, current_headers = url, dict(headers or {})
    visited: set[str] = set()
    client = None
    try:
        for hop in range(max_redirects + 1):
            hop_policy = _check_hop(
                current_url, policy=policy, require_https=require_https,
                trusted_hop=trusted_origin and hop == 0,
            )
            visit_key = f"GET {current_url}"
            if visit_key in visited:
                raise SsrFBlockedError(f"Redirect loop detected: {current_url}")
            visited.add(visit_key)
            if client is not None:
                await client.aclose()
            # Per-hop client (see _fetch_streaming): the hop's resolved policy
            # must reach the connect-time check, not just the pre-flight verdict.
            client = create_ssrf_safe_async_client(timeout=timeout, follow_redirects=False, policy=hop_policy)
            stream = client.stream("GET", current_url, headers=current_headers)
            response = await stream.__aenter__()
            try:
                if response.status_code not in _REDIRECT_STATUSES:
                    body = await response.aread()
                    await stream.__aexit__(None, None, None)
                    if max_bytes is not None and len(body) > max_bytes:
                        raise SsrFBlockedError(f"Blocked: response exceeded max_bytes ({max_bytes})")
                    return GuardedFetchResult(response=response, final_url=current_url, _close=None)
                if hop == max_redirects:
                    raise SsrFBlockedError(f"Too many redirects (limit: {max_redirects})")
                location = response.headers.get("location")
                if not location:
                    raise SsrFBlockedError(f"Redirect missing location header ({response.status_code})")
                next_url = urljoin(current_url, location)
                next_parsed, current_parsed = urlparse(next_url), urlparse(current_url)
                if (next_parsed.scheme, next_parsed.hostname, next_parsed.port) != (
                        current_parsed.scheme, current_parsed.hostname, current_parsed.port):
                    current_headers = _strip_sensitive_headers(current_headers)
                current_url = next_url
            finally:
                if response.status_code in _REDIRECT_STATUSES:
                    await stream.__aexit__(None, None, None)
        raise SsrFBlockedError(f"Too many redirects (limit: {max_redirects})")
    finally:
        if client is not None:
            await client.aclose()
