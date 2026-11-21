"""URL privacy routing for ``browser_exec`` (hybrid local routing).

``hybrid_routes_locally`` decides whether a URL belongs on the hybrid LOCAL sidecar instead of the
cloud provider; ``url_is_private`` is the routing oracle behind it. There is no refusal here: a URL
the agent asks the browser to open is opened.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from typing import Optional

from tools.url_safety import _is_declared_fake_ip

logger = logging.getLogger(__name__)

_PRIVATE_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".internal")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _private_ip(host: str) -> Optional[bool]:
    """None when ``host`` is not an IP literal."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if _is_declared_fake_ip(ip):
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in _CGNAT


def url_is_private(url: str) -> bool:
    """True when the URL's host is (or resolves to) a private/LAN/loopback/CGNAT address.

    Routing oracle only: DNS failures are NOT private (the configured backend surfaces the error). A local
    proxy's declared fake-ip sentinel (``security.fake_ip_ranges``) is not private: the name is public and
    the cloud browser resolves it itself."""
    try:
        hostname = (urllib.parse.urlparse(url).hostname or "").strip().lower().rstrip(".")
        if not hostname:
            return False
        literal = _private_ip(hostname)
        if literal is not None:
            return literal
        if hostname == "localhost" or hostname.endswith(_PRIVATE_HOST_SUFFIXES):
            return True
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False
        return any(_private_ip(sockaddr[0]) for *_, sockaddr in addr_info)
    except (ValueError, OSError, UnicodeError) as exc:
        logger.debug("URL-privacy check failed for %s: %s", url, exc)
        return False


def hybrid_routes_locally(url: str) -> bool:
    """True when ``url`` belongs on the hybrid LOCAL sidecar instead of the cloud provider: a provider
    configured, ``browser.auto_local_for_private_urls`` on, a private URL, and no CDP override (it owns
    the whole session)."""
    from tools.browser_session_cdp import get_cdp_override_raw
    from tools.browser_session_cloud import auto_local_for_private_urls, get_cloud_provider
    return (
        not get_cdp_override_raw()
        and get_cloud_provider() is not None
        and auto_local_for_private_urls()
        and url_is_private(url)
    )
