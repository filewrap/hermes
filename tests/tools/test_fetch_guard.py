"""Unified SSRF fetch guard (``tools/fetch_guard.py``, ported shape).

Behaviour contracts: policy verdicts on the pre-flight check, the manual
redirect loop (loop detection, redirect cap, header stripping), and the
``trusted_origin`` first-hop rule — all against loopback listeners, never the
network.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.fetch_guard import (
    SsrFBlockedError,
    async_is_url_allowed,
    fetch_with_ssrf_guard,
    is_url_allowed,
    stream_with_ssrf_guard,
)
from tools.ssrf_policy import SsrFPolicy
from tools.url_safety import _reset_allow_private_cache


@pytest.fixture
def allow_private(monkeypatch):
    """Loopback fetches need the private-URL toggle on, exactly as the
    pre-existing SSRF tests do it."""
    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "1")
    _reset_allow_private_cache()
    yield
    _reset_allow_private_cache()


class _Handler(BaseHTTPRequestHandler):
    hits: list[tuple[str, str | None]] = []
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.hits.append((self.path, self.headers.get("Authorization")))
        status, headers, body = self.routes.get(
            self.path, (200, {"Content-Type": "text/plain"}, b"ok"))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def listener(monkeypatch):
    _Handler.hits = []
    _Handler.routes = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", _Handler.hits, _Handler.routes
    httpd.shutdown()
    httpd.server_close()


# ── Verdicts ──────────────────────────────────────────────────────────────────


def test_loopback_and_metadata_blocked_by_default():
    assert not is_url_allowed("http://127.0.0.1:8080/x")
    assert not is_url_allowed("http://169.254.169.254/latest/meta-data/")
    assert not is_url_allowed("http://localhost:8080/x")
    assert not is_url_allowed("http://metadata.google.internal/")


def test_public_ip_literal_allowed_by_default():
    assert is_url_allowed("http://8.8.8.8/x")


def test_policy_hostname_gates_run_before_dns():
    blocked = SsrFPolicy(blocked_hostnames=("8.8.8.8",))
    assert not is_url_allowed("http://8.8.8.8/x", policy=blocked)  # blocked before any lookup
    allowed = SsrFPolicy(allowed_hostnames=("127.0.0.1",))
    assert is_url_allowed("http://127.0.0.1:8080/x", policy=allowed)


def test_hostname_allowlist_wildcard_never_matches_apex():
    from tools.ssrf_policy import matches_hostname_allowlist

    assert matches_hostname_allowlist("api.example.com", ("*.example.com",))
    assert not matches_hostname_allowlist("example.com", ("*.example.com",))
    assert not matches_hostname_allowlist("evil-example.com", ("*.example.com",))


def test_allowed_origins_promotes_only_that_hop():
    policy = SsrFPolicy(allowed_origins=("http://127.0.0.1:8080",))
    assert is_url_allowed("http://127.0.0.1:8080/x", policy=policy)
    assert not is_url_allowed("http://127.0.0.1:9090/x", policy=policy)


@pytest.mark.asyncio
async def test_async_verdict_matches_sync():
    assert await async_is_url_allowed("http://8.8.8.8/x")
    assert not await async_is_url_allowed("http://127.0.0.1:8080/x")


# ── Redirect loop ─────────────────────────────────────────────────────────────


def test_fetch_follows_redirects_and_reports_final_url(listener, allow_private):
    base, hits, routes = listener
    routes["/start"] = (302, {"Location": "/final"}, b"")
    routes["/final"] = (200, {"Content-Type": "text/plain"}, b"done")
    result = fetch_with_ssrf_guard(f"{base}/start", timeout=5)
    assert result.response.text == "done"
    assert result.final_url == f"{base}/final"
    assert [path for path, _ in hits] == ["/start", "/final"]


def test_fetch_detects_redirect_loop(listener, allow_private):
    base, _hits, routes = listener
    routes["/a"] = (302, {"Location": "/b"}, b"")
    routes["/b"] = (302, {"Location": "/a"}, b"")
    with pytest.raises(SsrFBlockedError, match="Redirect loop"):
        fetch_with_ssrf_guard(f"{base}/a", timeout=5)


def test_fetch_enforces_redirect_cap(listener, allow_private):
    base, _hits, routes = listener
    for i in range(5):
        routes[f"/hop{i}"] = (302, {"Location": f"/hop{i + 1}"}, b"")
    routes["/hop5"] = (200, {"Content-Type": "text/plain"}, b"done")
    with pytest.raises(SsrFBlockedError, match="Too many redirects"):
        fetch_with_ssrf_guard(f"{base}/hop0", timeout=5, max_redirects=3)


def test_redirect_to_metadata_blocked_after_first_hop(listener, allow_private):
    base, hits, routes = listener
    routes["/to-metadata"] = (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b"")
    with pytest.raises(SsrFBlockedError):
        fetch_with_ssrf_guard(f"{base}/to-metadata", timeout=5)
    assert [path for path, _ in hits] == ["/to-metadata"]  # the metadata hop never connects


def test_auth_header_never_survives_a_redirect(listener, allow_private):
    base, hits, routes = listener
    routes["/start"] = (302, {"Location": "/final"}, b"")
    routes["/final"] = (200, {"Content-Type": "text/plain"}, b"done")
    result = stream_with_ssrf_guard(
        f"{base}/start", timeout=5, headers={"Authorization": "Bearer test"})
    try:
        assert result.response.read() == b"done"
    finally:
        result.close()
    assert hits[0] == ("/start", "Bearer test")
    assert hits[1] == ("/final", None)


def test_fetch_enforces_max_bytes(listener, allow_private):
    base, _hits, routes = listener
    routes["/big"] = (200, {"Content-Type": "text/plain"}, b"x" * 100)
    with pytest.raises(SsrFBlockedError, match="max_bytes"):
        fetch_with_ssrf_guard(f"{base}/big", timeout=5, max_bytes=10)


# ── trusted_origin ────────────────────────────────────────────────────────────


def test_trusted_origin_allows_first_hop_only(listener):
    base, hits, routes = listener
    routes["/content"] = (200, {"Content-Type": "text/plain"}, b"clip")
    routes["/bounce"] = (302, {"Location": "/content"}, b"")
    result = fetch_with_ssrf_guard(f"{base}/content", timeout=5, trusted_origin=True)
    assert result.response.text == "clip"
    with pytest.raises(SsrFBlockedError):
        fetch_with_ssrf_guard(f"{base}/bounce", timeout=5, trusted_origin=True)
    assert [path for path, _ in hits] == ["/content", "/bounce"]  # hop 2 refused before connecting
    with pytest.raises(SsrFBlockedError, match="always-blocked"):
        fetch_with_ssrf_guard("http://169.254.169.254/latest/meta-data", timeout=5, trusted_origin=True)
