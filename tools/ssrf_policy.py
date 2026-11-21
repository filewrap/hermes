"""SSRF policy: the per-URL policy shape the external agent's fetch guard resolves, in
Hermes form. Pure data + hostname matching — no imports from ``tools`` so both
``tools/url_safety.py`` (the verdict owner) and ``tools/fetch_guard.py`` (the
fetch loop) can share it without a cycle.

A policy is permissive by omission: an empty ``hostname_allowlist`` allows
every hostname, and private-network blocking stays on unless
``allow_private_network`` (or an ``allowed_hostnames`` entry for the exact
host) switches it off. Operator denials (``blocked_hostnames``) run before any
DNS, as in the external agent's ``resolveHostnamePolicyChecks``. The cloud-metadata
floor is not a policy field: ``url_safety`` enforces it unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


def normalize_hostname(hostname: str) -> str:
    """Lowercase, trailing dots dropped, bracketed IPv6 unbracketed — the form
    every policy comparison uses, mirroring the external agent's ``normalizeHostname``."""
    normalized = (hostname or "").strip().lower().rstrip(".")
    if normalized.startswith("[") and normalized.endswith("]"):
        return normalized[1:-1]
    return normalized


@dataclass(frozen=True)
class SsrFPolicy:
    """Per-URL SSRF policy (external-agent ``SsrFPolicy``)."""

    allow_private_network: bool = False
    allow_rfc2544_benchmark_range: bool = False
    allow_ipv6_unique_local_range: bool = False
    allowed_hostnames: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    hostname_allowlist: tuple[str, ...] = ()
    blocked_hostnames: tuple[str, ...] = ()

    def with_allowed_host(self, hostname: str) -> SsrFPolicy:
        """A copy with *hostname* added to ``allowed_hostnames``."""
        normalized = normalize_hostname(hostname)
        if normalized in [normalize_hostname(h) for h in self.allowed_hostnames]:
            return self
        return SsrFPolicy(
            allow_private_network=self.allow_private_network,
            allow_rfc2544_benchmark_range=self.allow_rfc2544_benchmark_range,
            allow_ipv6_unique_local_range=self.allow_ipv6_unique_local_range,
            allowed_hostnames=(*self.allowed_hostnames, normalized),
            allowed_origins=self.allowed_origins,
            hostname_allowlist=self.hostname_allowlist,
            blocked_hostnames=self.blocked_hostnames,
        )


def _normalize_origin(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    scheme = parsed.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    port = parsed.port
    return f"{scheme}://{normalize_hostname(parsed.hostname)}" + (
        f":{port}" if port and port != default_port else ""
    )


def resolve_policy_for_url(url: str, policy: SsrFPolicy | None) -> SsrFPolicy | None:
    """The policy for one URL: an ``allowed_origins`` entry matching the URL's
    origin promotes only that URL's hostname into ``allowed_hostnames``.
    Redirects re-resolve per hop, so trust never carries across origins."""
    if policy is None or not policy.allowed_origins:
        return policy
    try:
        origin = _normalize_origin(url)
    except ValueError:
        return policy
    if not origin:
        return policy
    allowed = {_normalize_origin(o) for o in policy.allowed_origins}
    if origin not in allowed:
        return policy
    try:
        hostname = normalize_hostname(urlparse(url).hostname or "")
    except ValueError:
        return policy
    return policy.with_allowed_host(hostname) if hostname else policy


def _matches_pattern(hostname: str, pattern: str) -> bool:
    """Exact match, or ``*.example.com`` for subdomains (never the apex)."""
    pattern = normalize_hostname(pattern)
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return bool(suffix) and hostname != suffix and hostname.endswith(f".{suffix}")
    return hostname == pattern


def matches_hostname_allowlist(hostname: str, allowlist: tuple[str, ...] | list[str]) -> bool:
    """True when the allowlist is empty (no restriction) or a pattern matches."""
    if not allowlist:
        return True
    normalized = normalize_hostname(hostname)
    return any(_matches_pattern(normalized, pattern) for pattern in allowlist)


def policy_blocks_hostname(hostname: str, policy: SsrFPolicy | None) -> str | None:
    """Why *policy* refuses *hostname* before any DNS, else None."""
    if policy is None:
        return None
    normalized = normalize_hostname(hostname)
    if not normalized:
        return "empty hostname"
    if any(_matches_pattern(normalized, pattern) for pattern in policy.blocked_hostnames):
        return f"blocked hostname (configured blocklist): {hostname}"
    if not matches_hostname_allowlist(normalized, policy.hostname_allowlist):
        permitted = ", ".join(policy.hostname_allowlist)
        return f"blocked hostname (not in allowlist): {hostname}. Permitted: {permitted}"
    return None


def policy_allows_private_network(hostname: str, policy: SsrFPolicy | None) -> bool:
    """True when *policy* exempts *hostname* from private-IP blocking."""
    if policy is None:
        return False
    if policy.allow_private_network:
        return True
    normalized = normalize_hostname(hostname)
    return normalized in [normalize_hostname(h) for h in policy.allowed_hostnames]


def policy_from_config(config: dict | None) -> SsrFPolicy | None:
    """The SSRF policy declared under ``security.ssrf`` in config, or None when
    the section is absent (callers then use the global ``allow_private_urls``
    toggle and fake-ip ranges, exactly as before)."""
    if not isinstance(config, dict):
        return None
    section = config.get("security") or {}
    ssrf = section.get("ssrf") if isinstance(section, dict) else None
    if not isinstance(ssrf, dict) or not ssrf:
        return None

    def _names(key: str) -> tuple[str, ...]:
        raw = ssrf.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(str(v).strip() for v in raw if str(v).strip())

    return SsrFPolicy(
        allow_private_network=bool(ssrf.get("allow_private_network", False)),
        allow_rfc2544_benchmark_range=bool(ssrf.get("allow_rfc2544_benchmark_range", False)),
        allow_ipv6_unique_local_range=bool(ssrf.get("allow_ipv6_unique_local_range", False)),
        allowed_hostnames=_names("allowed_hostnames"),
        allowed_origins=_names("allowed_origins"),
        hostname_allowlist=_names("hostname_allowlist"),
        blocked_hostnames=_names("blocked_hostnames"),
    )
