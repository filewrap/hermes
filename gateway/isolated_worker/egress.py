"""Narrow, identity-bound HTTP(S) egress for isolated workers.

This module is a request policy and direct transport, not a network sandbox: it
cannot stop other worker code from opening sockets. Production workers must have
no direct network route and reach the network only through an externally enforced
service running this policy. No gateway/browser/MCP client or proxy is reused.
Trusted deployment code should populate ``deny_hosts``/``deny_networks`` with
host public addresses and any shared authenticated browser, MCP, or connector
endpoints; these rules apply even when those endpoints use public HTTPS ports.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from . import Binding, BoundaryDenied, Peer


_MAX_URL_LENGTH = 8192
_MAX_BODY_BYTES = 5 * 1024 * 1024
_MAX_TIMEOUT = 30.0
_MAX_REDIRECTS = 5
_MAX_DNS_ANSWERS = 32
_MAX_HEADER_COUNT = 100
_MAX_HEADER_BYTES = 64 * 1024
_MAX_REQUEST_HEADER_BYTES = 8 * 1024

# These ports are not part of public web access. In particular, do not turn a
# URL fetcher into access to common browser-debug or local control services.
_ALLOWED_PORTS = {80, 443}
_BLOCKED_HOSTS = {
    "metadata", "metadata.google.internal", "metadata.azure.com",
    "instance-data.ec2.internal", "metadata.tencentyun.com",
    "metadata.oraclecloud.com", "host.docker.internal",
    "gateway.docker.internal", "docker.for.mac.host.internal",
    "host.containers.internal", "gateway.containers.internal",
    "kubernetes.default.svc", "kubernetes.default", "localhost",
    "ip6-localhost", "browser", "chrome", "chromium", "playwright",
    "selenium", "mcp", "gateway",
}
_BLOCKED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".home.arpa", ".svc",
    ".cluster.local", ".intranet", ".lan", ".onion",
)
_SAFE_REQUEST_HEADERS = {"accept", "accept-language", "range", "user-agent"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_IPV6_TRANSITION_NETWORKS = tuple(map(ipaddress.ip_network, (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
)))


@dataclass(frozen=True)
class EgressResponse:
    """A bounded response; headers are immutable name/value pairs."""

    status: int
    reason: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    url: str


class _PinnedConnectionMixin:
    """Connect to an already checked numeric address, never resolving again."""

    def __init__(self, host: str, port: int, address: str, dialer, timeout: float, *args, **kwargs):
        self._pinned_address = address
        self._dialer = dialer
        super().__init__(host, port, timeout=timeout, *args, **kwargs)

    def connect(self):
        # The dialer receives only the validated numeric address. Its default
        # implementation never consults proxy environment variables.
        self.sock = self._dialer(self._pinned_address, self.port, self.timeout)


class _PinnedHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    def connect(self):
        raw = self._dialer(self._pinned_address, self.port, self.timeout)
        # Keep the canonical URL hostname for SNI and certificate verification;
        # only the TCP destination is pinned to the prevalidated IP.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class EgressClient:
    """Direct public-web client bound to a supervisor-attested peer.

    ``authorize`` must consult current server-side authorization for the peer's
    immutable binding. The expected ``binding`` is supplied by trusted service
    code; it is compared with the attested ``peer.binding`` before any request.
    Resolver/dialer overrides are trusted construction-time seams for tests and
    controlled deployments, never request parameters supplied by a worker.
    """

    def __init__(
        self,
        authorize: Callable[[Peer], bool],
        *,
        resolver: Callable[..., list] | None = None,
        dialer: Callable[[str, int, float], socket.socket] | None = None,
        deny_hosts: Iterable[str] = (),
        deny_networks: Iterable[str | ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
    ):
        if not callable(authorize):
            raise TypeError("authorize must be callable")
        self._authorize = authorize
        self._resolver = resolver or socket.getaddrinfo
        self._dialer = dialer or self._direct_dial
        self._ssl_context = ssl.create_default_context()
        if isinstance(deny_hosts, str):
            deny_hosts = (deny_hosts,)
        if isinstance(deny_networks, (str, ipaddress.IPv4Network, ipaddress.IPv6Network)):
            deny_networks = (deny_networks,)
        try:
            self._deny_hosts = frozenset(self._normalize_deny_host(host) for host in deny_hosts)
            self._deny_networks = tuple(
                network if isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network))
                else ipaddress.ip_network(network, strict=False)
                for network in deny_networks
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid egress deny rule") from exc

    @staticmethod
    def _direct_dial(address: str, port: int, timeout: float):
        return socket.create_connection((address, port), timeout=timeout)

    @staticmethod
    def _normalize_deny_host(host: str) -> str:
        if not isinstance(host, str) or not host or "%" in host or "*" in host:
            raise ValueError("deny_hosts requires exact hostnames or IPs")
        try:
            return ipaddress.ip_address(host).compressed.lower()
        except ValueError:
            normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
            if not normalized or any(not label for label in normalized.split(".")):
                raise ValueError("invalid deny hostname")
            return normalized

    def request(
        self,
        peer: Peer,
        binding: Binding,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 1024 * 1024,
        max_redirects: int = 3,
    ) -> EgressResponse:
        """Fetch a public URL with no credentials, proxying, or implicit redirects.

        Only GET/HEAD are supported. Each redirect starts a fresh validation and
        DNS lookup, and HTTPS-to-HTTP downgrade redirects are refused. A response
        over the byte/header limits is closed and denied rather than truncated.
        """
        self._check_peer(peer, binding)
        if method not in {"GET", "HEAD"}:
            raise BoundaryDenied("Only GET and HEAD are allowed")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= _MAX_TIMEOUT:
            raise BoundaryDenied("Invalid request timeout")
        if type(max_bytes) is not int or not 0 <= max_bytes <= _MAX_BODY_BYTES:
            raise BoundaryDenied("Invalid response size limit")
        if type(max_redirects) is not int or not 0 <= max_redirects <= _MAX_REDIRECTS:
            raise BoundaryDenied("Invalid redirect limit")
        clean_headers = self._validate_headers(headers or {})

        current_url = url
        followed = 0
        while True:
            self._check_peer(peer, binding)
            scheme, host, port, target, authority = self._parse_url(current_url)
            if host in self._deny_hosts:
                raise BoundaryDenied("Destination hostname is denied by policy")
            addresses = self._resolve(host, port)
            self._check_peer(peer, binding)
            response = self._exchange(
                scheme, host, port, addresses[0], target, authority,
                method, clean_headers, float(timeout), max_bytes,
            )
            self._check_peer(peer, binding)

            if response.status not in _REDIRECT_STATUSES:
                return EgressResponse(
                    response.status, response.reason, response.headers,
                    response.body, current_url,
                )
            locations = [value for name, value in response.headers if name.lower() == "location"]
            if len(locations) > 1:
                raise BoundaryDenied("Ambiguous redirect response")
            if not locations:
                return EgressResponse(
                    response.status, response.reason, response.headers,
                    response.body, current_url,
                )
            if followed >= max_redirects:
                raise BoundaryDenied("Redirect limit exceeded")
            next_url = urljoin(current_url, locations[0])
            next_scheme, _, _, _, _ = self._parse_url(next_url)
            if scheme == "https" and next_scheme != "https":
                raise BoundaryDenied("HTTPS downgrade redirect refused")
            current_url = next_url
            followed += 1

    def _check_peer(self, peer: Peer, binding: Binding) -> None:
        if not isinstance(peer, Peer) or not isinstance(binding, Binding) or peer.binding != binding:
            raise BoundaryDenied("Worker identity mismatch")
        try:
            allowed = self._authorize(peer)
        except Exception as exc:
            raise BoundaryDenied("Worker authorization unavailable") from exc
        if allowed is not True:
            raise BoundaryDenied("Worker is not authorized")

    @staticmethod
    def _validate_headers(headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping):
            raise BoundaryDenied("Invalid request headers")
        result: dict[str, str] = {}
        seen: set[str] = set()
        total = 0
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise BoundaryDenied("Invalid request headers")
            lowered = name.lower()
            if lowered not in _SAFE_REQUEST_HEADERS or lowered in seen:
                raise BoundaryDenied("Request header is not permitted")
            if not name.isascii() or not name.replace("-", "").isalnum() or any(ord(c) < 33 for c in name):
                raise BoundaryDenied("Invalid request header name")
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise BoundaryDenied("Invalid request header value")
            total += len(name.encode("ascii")) + len(value.encode("utf-8")) + 4
            if total > _MAX_REQUEST_HEADER_BYTES:
                raise BoundaryDenied("Request headers are too large")
            seen.add(lowered)
            result[name] = value
        return result

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str, int, str, str]:
        if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
            raise BoundaryDenied("Invalid URL")
        if any(ord(char) < 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise BoundaryDenied("Invalid URL")
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.lower()
            if scheme not in {"http", "https"}:
                raise BoundaryDenied("Only HTTP(S) URLs are allowed")
            if not parsed.netloc or "@" in parsed.netloc:
                raise BoundaryDenied("URL userinfo is not allowed")
            raw_host = parsed.hostname
            port = parsed.port
        except (ValueError, UnicodeError) as exc:
            raise BoundaryDenied("Malformed URL authority") from exc
        if not raw_host or "%" in raw_host:
            # Percent-encoded hostnames and IPv6 zone identifiers have ambiguous
            # interpretations across URL parsers and are deliberately rejected.
            raise BoundaryDenied("Invalid URL hostname")
        try:
            literal = ipaddress.ip_address(raw_host)
        except ValueError:
            literal = None
        if literal is not None:
            host = literal.compressed.lower()
            if not EgressClient._is_public_ip(literal):
                raise BoundaryDenied("Destination address is not public")
        else:
            try:
                host = raw_host.encode("idna").decode("ascii").lower().rstrip(".")
            except (UnicodeError, ValueError) as exc:
                raise BoundaryDenied("Invalid URL hostname") from exc
            if not host or len(host) > 253 or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label)
                for label in host.split(".")
            ):
                raise BoundaryDenied("Invalid URL hostname")
            if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
                raise BoundaryDenied("Destination hostname is not allowed")
        port = port if port is not None else (443 if scheme == "https" else 80)
        if port not in _ALLOWED_PORTS or (scheme == "https" and port != 443) or (scheme == "http" and port != 80):
            raise BoundaryDenied("Destination port is not allowed")
        path = parsed.path or "/"
        target = path + (("?" + parsed.query) if parsed.query else "")
        if not target.startswith("/") or "\r" in target or "\n" in target:
            raise BoundaryDenied("Invalid request target")
        authority = host
        if port != (443 if scheme == "https" else 80):
            authority = f"[{host}]" if ":" in host else host
            authority += f":{port}"
        elif ":" in host:
            authority = f"[{host}]"
        return scheme, host, port, target, authority

    @staticmethod
    def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return False
        if isinstance(address, ipaddress.IPv6Address) and any(
            address in network for network in _IPV6_TRANSITION_NETWORKS
        ):
            return False
        return bool(
            address.is_global
            and not address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_reserved
            and not address.is_multicast
            and not address.is_unspecified
        )

    def _resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            answers = self._resolver(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            raise BoundaryDenied("Destination could not be resolved") from exc
        found: list[str] = []
        for index, answer in enumerate(answers):
            if index >= _MAX_DNS_ANSWERS:
                raise BoundaryDenied("DNS answer set is too large")
            try:
                sockaddr = answer[4]
                raw_address = sockaddr[0]
                if not isinstance(raw_address, str) or "%" in raw_address:
                    raise ValueError("invalid address")
                address = ipaddress.ip_address(raw_address)
            except (IndexError, TypeError, ValueError) as exc:
                raise BoundaryDenied("Invalid DNS answer") from exc
            if not self._is_public_ip(address):
                # Reject the entire DNS set, not just the unsafe member. This
                # prevents a mixed public/private answer from hiding a target.
                raise BoundaryDenied("DNS answer contains a non-public address")
            if any(address.version == network.version and address in network for network in self._deny_networks):
                raise BoundaryDenied("DNS answer is denied by destination policy")
            numeric = address.compressed
            if numeric not in found:
                found.append(numeric)
        if not found:
            raise BoundaryDenied("Destination has no usable addresses")
        return tuple(found)

    def _exchange(
        self, scheme: str, host: str, port: int, address: str,
        target: str, authority: str, method: str, headers: dict[str, str],
        timeout: float, max_bytes: int,
    ) -> EgressResponse:
        connection_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
        kwargs = {"context": self._ssl_context} if scheme == "https" else {}
        connection = connection_cls(host, port, address, self._dialer, timeout, **kwargs)
        deadline_timer = None
        try:
            # Construct the request explicitly to control Host for IPv6 literals
            # and avoid duplicate or caller-supplied authority headers.
            connection.putrequest(method, target, skip_host=True)
            connection.putheader("Host", authority)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            deadline = time.monotonic() + timeout
            if connection.sock is not None:
                deadline_timer = threading.Timer(timeout, self._interrupt_socket, (connection.sock,))
                deadline_timer.daemon = True
                deadline_timer.start()
            response = connection.getresponse()
            raw_headers = response.getheaders()
            if len(raw_headers) > _MAX_HEADER_COUNT or sum(
                len(name.encode("latin-1", "replace")) + len(value.encode("latin-1", "replace")) + 4
                for name, value in raw_headers
            ) > _MAX_HEADER_BYTES:
                raise BoundaryDenied("Response headers are too large")
            content_length = response.getheader("Content-Length")
            if content_length is not None and method != "HEAD":
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise BoundaryDenied("Malformed response length") from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise BoundaryDenied("Response exceeds size limit")
            body = b""
            if method != "HEAD":
                chunks: list[bytes] = []
                total = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BoundaryDenied("Response timed out")
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise BoundaryDenied("Response exceeds size limit")
                body = b"".join(chunks)
            return EgressResponse(
                response.status, response.reason or "", tuple(raw_headers), body, "",
            )
        except BoundaryDenied:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
            raise BoundaryDenied("Outbound request failed") from exc
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            connection.close()

    @staticmethod
    def _interrupt_socket(sock: socket.socket) -> None:
        # A timer is needed in addition to socket inactivity timeouts: a peer
        # sending one byte at a time must not extend the overall response clock.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            return
