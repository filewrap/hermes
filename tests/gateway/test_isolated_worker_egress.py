"""Behavioral coverage for isolated-worker public-web egress policy."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import threading
import time

import pytest

from gateway.isolated_worker import Binding, BoundaryDenied, Peer
from gateway.isolated_worker.egress import EgressClient, _PinnedHTTPSConnection


PUBLIC_IP = "93.184.216.34"


def make_peer(identity: str = "alice") -> tuple[Binding, Peer]:
    binding = Binding(
        identity=identity,
        authorization_stamp="a" * 64,
        chat_id="room-1",
        chat_type="group",
    )
    peer = Peer(
        worker_id=("1" if identity == "alice" else "2") * 32,
        binding=binding,
        uid=1234 if identity == "alice" else 1235,
        cgroup="/hermes-workers/" + (("1" if identity == "alice" else "2") * 32),
    )
    return binding, peer


class ResponseServer:
    """A local HTTP server reached only through the test's fake public dialer."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._respond()

            def do_HEAD(self):
                self._respond(head=True)

            def _respond(self, head=False):
                owner.seen.append((self.command, self.path, dict(self.headers.items())))
                status, headers, body = owner.routes.get(self.path, (200, {}, b"ok"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if not any(key.lower() == "content-length" for key in headers):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture

def transport():
    server = ResponseServer()
    dials = []
    lookups = []
    dns = {}

    def resolver(host, port, *, type):
        lookups.append((host, port, type))
        addresses = dns.get(host, [PUBLIC_IP])
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             (ip, port, 0, 0) if ":" in ip else (ip, port))
            for ip in addresses
        ]

    def dialer(address, port, timeout):
        dials.append((address, port))
        # Deliberately map only after the policy has pinned a public IP. The
        # local endpoint is test infrastructure, not a production route.
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    binding, peer = make_peer()
    client = EgressClient(lambda _peer: True, resolver=resolver, dialer=dialer)
    yield server, dns, dials, lookups, binding, peer, client
    server.close()


def test_public_request_uses_resolved_numeric_destination_and_no_credentials(transport, monkeypatch):
    server, _dns, dials, lookups, binding, peer, client = transport
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    result = client.request(
        peer, binding, "http://public.example/path?q=1",
        headers={"Accept": "text/plain", "User-Agent": "worker-web/1"},
    )
    assert (result.status, result.body, result.url) == (
        200, b"ok", "http://public.example/path?q=1",
    )
    assert dials == [(PUBLIC_IP, 80)]
    assert lookups == [("public.example", 80, socket.SOCK_STREAM)]
    request_headers = server.seen[0][2]
    assert request_headers["Host"] == "public.example"
    assert request_headers["Accept"] == "text/plain"
    assert "Authorization" not in request_headers and "Cookie" not in request_headers


def test_identity_binding_mismatch_is_denied_before_dns_or_dial(transport):
    _server, _dns, dials, lookups, _binding, _peer, client = transport
    binding_b, _peer_b = make_peer("bob")
    _binding_a, peer_a = make_peer("alice")
    with pytest.raises(BoundaryDenied, match="identity"):
        client.request(peer_a, binding_b, "http://public.example/")
    assert dials == [] and lookups == []


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.2.3.4/", "http://172.20.1.1/",
    "http://192.168.1.1/", "http://169.254.169.254/", "http://224.0.0.1/",
    "http://0.0.0.0/", "http://[::1]/", "http://[fe80::1]/",
    "http://[fc00::1]/", "http://[::ffff:127.0.0.1]/",
])
def test_non_global_ip_literals_are_denied(transport, url):
    _server, _dns, dials, _lookups, binding, peer, client = transport
    with pytest.raises(BoundaryDenied):
        client.request(peer, binding, url)
    assert dials == []


@pytest.mark.parametrize("host", [
    "metadata.google.internal", "metadata.azure.com", "instance-data.ec2.internal",
    "host.docker.internal", "localhost", "browser", "mcp",
])
def test_metadata_and_shared_service_names_are_denied(transport, host):
    _server, _dns, dials, _lookups, binding, peer, client = transport
    with pytest.raises(BoundaryDenied):
        client.request(peer, binding, f"http://{host}/")
    assert dials == []


@pytest.mark.parametrize("answers", [
    ["93.184.216.34", "10.0.0.8"],
    ["93.184.216.34", "169.254.169.254"],
    ["93.184.216.34", "::ffff:127.0.0.1"],
    ["2001:4860:4860::8888", "fd00::1"],
])
def test_dns_answer_set_must_be_entirely_public(transport, answers):
    _server, dns, dials, _lookups, binding, peer, client = transport
    dns["mixed.example"] = answers
    with pytest.raises(BoundaryDenied, match="non-public"):
        client.request(peer, binding, "http://mixed.example/")
    assert dials == []


def test_public_ipv6_dns_answer_is_pinned_as_ipv6(transport):
    _server, dns, dials, _lookups, binding, peer, client = transport
    dns["v6.example"] = ["2001:4860:4860::8888"]
    # The fake dialer deliberately raises after proving the selected numeric
    # address is the validated IPv6 answer.
    def fail_dial(address, port, timeout):
        dials.append((address, port))
        raise OSError("synthetic no route")

    client = EgressClient(lambda _peer: True, resolver=lambda host, port, type: [
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
         ("2001:4860:4860::8888", port, 0, 0))
    ], dialer=fail_dial)
    with pytest.raises(BoundaryDenied, match="request failed"):
        client.request(peer, binding, "http://v6.example/")
    assert dials == [("2001:4860:4860::8888", 80)]


@pytest.mark.parametrize("address", [
    "64:ff9b::808:808", "64:ff9b:1::808:808", "2002:0808:0808::1",
    "2001:0000:0808:0808::1",
])
def test_ipv6_transition_and_nat64_ranges_are_not_public_destinations(transport, address):
    _server, _dns, dials, _lookups, binding, peer, _client = transport
    client = EgressClient(lambda _peer: True, resolver=lambda host, port, type: [
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port, 0, 0))
    ], dialer=lambda ip, port, timeout: dials.append((ip, port)))
    with pytest.raises(BoundaryDenied, match="non-public"):
        client.request(peer, binding, "http://transition.example/")
    assert dials == []


def test_trusted_host_and_network_deny_rules_block_public_endpoints(transport):
    _server, _dns, dials, lookups, binding, peer, _client = transport
    host_client = EgressClient(
        lambda _peer: True, resolver=lambda *args, **kwargs: pytest.fail("must not resolve"),
        dialer=lambda ip, port, timeout: pytest.fail("must not dial"),
        deny_hosts=["shared-browser.example"],
    )
    with pytest.raises(BoundaryDenied, match="hostname is denied"):
        host_client.request(peer, binding, "https://shared-browser.example/json/version")
    network_client = EgressClient(
        lambda _peer: True,
        resolver=lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))
        ],
        dialer=lambda ip, port, timeout: pytest.fail("must not dial"),
        deny_networks=["93.184.216.0/24"],
    )
    with pytest.raises(BoundaryDenied, match="denied by destination policy"):
        network_client.request(peer, binding, "https://shared-mcp.example/api")
    assert dials == [] and lookups == []


def test_redirect_to_trusted_denied_shared_endpoint_is_blocked(transport):
    server, _dns, dials, _lookups, binding, peer, _client = transport
    server.routes["/start"] = (302, {"Location": "https://shared-browser.example/json"}, b"")
    client = EgressClient(
        lambda _peer: True,
        resolver=lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))
        ],
        dialer=lambda ip, port, timeout: (
            dials.append((ip, port)) or socket.create_connection(("127.0.0.1", server.port), timeout)
        ),
        deny_hosts=["shared-browser.example"],
    )
    with pytest.raises(BoundaryDenied, match="hostname is denied"):
        client.request(peer, binding, "http://public.example/start")
    assert dials == [(PUBLIC_IP, 80)]


def test_dns_answer_count_is_bounded(transport):
    _server, _dns, dials, _lookups, binding, peer, _client = transport
    client = EgressClient(lambda _peer: True, resolver=lambda host, port, type: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))
        for _ in range(33)
    ], dialer=lambda ip, port, timeout: dials.append((ip, port)))
    with pytest.raises(BoundaryDenied, match="answer set is too large"):
        client.request(peer, binding, "http://many.example/")
    assert dials == []


@pytest.mark.parametrize("url", [
    "http://user:pass@public.example/", "http://public.example:99999/",
    "http://public.example:notaport/", "http://%31%32%37.0.0.1/",
    "http://public.example:81/", "ftp://public.example/", "ws://public.example/",
    "http://public.example\\@127.0.0.1/", "http://public.example/line\r\nbreak",
])
def test_malformed_or_unsupported_url_authorities_are_denied(transport, url):
    _server, _dns, dials, _lookups, binding, peer, client = transport
    with pytest.raises(BoundaryDenied):
        client.request(peer, binding, url)
    assert dials == []


@pytest.mark.parametrize("kwargs", [
    {"method": "CONNECT"}, {"method": "POST"},
    {"headers": {"Authorization": "Bearer secret"}},
    {"headers": {"Cookie": "sid=secret"}},
    {"headers": {"Proxy-Authorization": "secret"}},
    {"headers": {"Host": "127.0.0.1"}},
    {"headers": {"Upgrade": "websocket"}},
    {"headers": {"Connection": "Upgrade"}},
])
def test_methods_and_hop_by_hop_or_authenticated_headers_are_denied(transport, kwargs):
    _server, _dns, dials, _lookups, binding, peer, client = transport
    with pytest.raises(BoundaryDenied):
        client.request(peer, binding, "http://public.example/", **kwargs)
    assert dials == []


def test_redirect_is_re_resolved_and_private_target_is_never_dialed(transport):
    server, dns, dials, _lookups, binding, peer, _client = transport
    server.routes["/start"] = (302, {"Location": "http://private-hop.example/secret"}, b"")
    dns["private-hop.example"] = ["192.168.0.7"]
    client = EgressClient(
        lambda _peer: True,
        resolver=lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             (dns.get(host, [PUBLIC_IP])[0], port))
        ],
        dialer=lambda address, port, timeout: (
            dials.append((address, port)) or socket.create_connection(("127.0.0.1", server.port), timeout)
        ),
    )
    with pytest.raises(BoundaryDenied, match="non-public"):
        client.request(peer, binding, "http://public.example/start")
    assert dials == [(PUBLIC_IP, 80)]


def test_redirect_re_resolves_same_hostname_to_defend_against_rebinding(transport):
    server, _dns, dials, _lookups, binding, peer, _client = transport
    server.routes["/start"] = (302, {"Location": "http://public.example/next"}, b"")
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])
    client = EgressClient(
        lambda _peer: True,
        resolver=lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (next(answers)[0], port))
        ],
        dialer=lambda address, port, timeout: (
            dials.append((address, port)) or socket.create_connection(("127.0.0.1", server.port), timeout)
        ),
    )
    with pytest.raises(BoundaryDenied, match="non-public"):
        client.request(peer, binding, "http://public.example/start")
    assert dials == [(PUBLIC_IP, 80)]


def test_https_redirect_downgrade_is_denied(transport):
    server, _dns, dials, _lookups, binding, peer, _client = transport
    server.routes["/secure"] = (302, {"Location": "http://public.example/plain"}, b"")
    # Exercise redirect handling without external TLS; the synthetic HTTPS
    # connection's response is supplied by a small trusted test client.
    class RedirectingClient(EgressClient):
        def _exchange(self, *args, **kwargs):
            return type("Response", (), {
                "status": 302, "reason": "Found",
                "headers": (("Location", "http://public.example/plain"),), "body": b"",
            })()

    client = RedirectingClient(lambda _peer: True, resolver=lambda host, port, type: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))
    ], dialer=lambda address, port, timeout: dials.append((address, port)))
    with pytest.raises(BoundaryDenied, match="downgrade"):
        client.request(peer, binding, "https://public.example/secure")
    assert dials == []


def test_dns_is_looked_up_again_for_each_request_and_authorization_rechecked_after_response(transport):
    server, _dns, _dials, _lookups, binding, peer, client = transport
    checks = []

    def authorization(candidate):
        checks.append(candidate)
        return len(checks) < 3

    client = EgressClient(authorization, resolver=lambda host, port, type: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))
    ], dialer=lambda address, port, timeout: socket.create_connection(("127.0.0.1", server.port), timeout))
    with pytest.raises(BoundaryDenied, match="not authorized"):
        client.request(peer, binding, "http://public.example/")
    assert len(checks) == 3


def test_response_body_is_bounded_and_head_has_no_body(transport):
    server, _dns, _dials, _lookups, binding, peer, client = transport
    server.routes["/large"] = (200, {}, b"0123456789")
    with pytest.raises(BoundaryDenied, match="size limit"):
        client.request(peer, binding, "http://public.example/large", max_bytes=5)
    result = client.request(peer, binding, "http://public.example/large", method="HEAD", max_bytes=5)
    assert result.body == b""


def test_redirect_count_is_bounded(transport):
    server, _dns, _dials, _lookups, binding, peer, client = transport
    server.routes["/a"] = (302, {"Location": "/b"}, b"")
    server.routes["/b"] = (302, {"Location": "/a"}, b"")
    with pytest.raises(BoundaryDenied, match="Redirect limit"):
        client.request(peer, binding, "http://public.example/a", max_redirects=1)


def test_https_pins_tcp_but_verifies_tls_against_url_hostname():
    class RawSocket:
        def close(self):
            pass

    class Context:
        def __init__(self):
            self.server_names = []

        def wrap_socket(self, raw, *, server_hostname):
            self.server_names.append(server_hostname)
            return raw

    context = Context()
    dials = []
    connection = _PinnedHTTPSConnection(
        "www.example.com", 443, "93.184.216.34",
        lambda address, port, timeout: (dials.append((address, port)) or RawSocket()),
        3.0, context=ssl.create_default_context(),
    )
    connection._context = context
    connection.connect()
    connection.close()
    assert dials == [("93.184.216.34", 443)]
    assert context.server_names == ["www.example.com"]


def test_slow_response_headers_cannot_extend_total_deadline():
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.5)
            try:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            except OSError:
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    binding, peer = make_peer()
    client = EgressClient(
        lambda _peer: True,
        resolver=lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))
        ],
        dialer=lambda ip, port, timeout: socket.create_connection(("127.0.0.1", server.server_address[1]), timeout),
    )
    started = time.monotonic()
    try:
        with pytest.raises(BoundaryDenied):
            client.request(peer, binding, "http://slow.example/", timeout=0.1)
        assert time.monotonic() - started < 0.4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
