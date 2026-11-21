"""MCP server mapping for the reverse migration (Hermes -> the external agent).

Ports the mapping half of the external agent's ``extensions/migrate-hermes``
``config-mcp.ts`` (``planHermesMcpServer``) — the Hermes-to-external-agent
direction is the same one the forward provider already implements, so the
field mapping is shared: connection fields, transport names, timeouts,
tool filters, OAuth metadata, client certificates.

Secrets rule (reverse direction): a ``${VAR}`` reference passes through
untouched; a literal value under a credential-like key (``api_key``,
``token``, ``secret``, ``password``, ``auth`` …) is replaced by a
``${KEY}`` reference and listed as a manual item — the value is never
copied. Any other literal passes through, and the post-write secret scan
is the backstop: if a literal equals a harvested secret value, the export
fails validation instead of shipping it.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from hermes_cli.export_agent_model import resolve_env_references

MCP_CONNECTION_FIELDS = (
    "enabled",
    "command",
    "args",
    "cwd",
    "workingDirectory",
    "url",
    "connectionTimeoutMs",
    "requestTimeoutMs",
)

_RESOURCE_TOOLS = ("resources_list", "resources_read")
_PROMPT_TOOLS = ("prompts_list", "prompts_get")

_CREDENTIAL_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|auth|bearer|credential)", re.IGNORECASE
)


def _opt_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if (
        isinstance(value, (int, float))
        and value > 0
        and value == value
        and value not in (float("inf"), float("-inf"))
    ):
        return float(value)
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
    return None


def _tool_filter_list(value: Any) -> Optional[list[str]]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out = [
            entry.strip() for entry in value if isinstance(entry, str) and entry.strip()
        ]
        return sorted(set(out)) or None
    return None


def _has_unsupported_pattern(pattern: str) -> bool:
    """The external agent supports only exact names and ``*``; Hermes uses fnmatch."""
    return "?" in pattern or "[" in pattern


def _map_client_certificate(value: dict[str, Any]) -> dict[str, str]:
    cert = value.get("clientCert", value.get("client_cert"))
    key = _opt_str(value.get("clientKey")) or _opt_str(value.get("client_key"))
    if isinstance(cert, list) and len(cert) == 2:
        cert_path, key_path = _opt_str(cert[0]), _opt_str(cert[1])
        return (
            {"clientCert": cert_path, "clientKey": key_path}
            if cert_path and key_path
            else {}
        )
    cert_path = _opt_str(cert)
    return {"clientCert": cert_path, "clientKey": key} if cert_path and key else {}


def _map_oauth(value: dict[str, Any]) -> Optional[dict[str, Any]]:
    oauth = _record(value.get("oauth"))
    if not oauth:
        return None
    mapped = {}
    for key in ("authProfileId", "scope", "redirectUrl", "clientMetadataUrl"):
        found = _opt_str(oauth.get(key))
        if found:
            mapped[key] = found
    return mapped or None


def _redact_env_mapping(
    mapping: dict[str, Any],
    *,
    kind: str,
    name: str,
    manual: list[dict[str, str]],
    source: str,
) -> tuple[dict[str, Any], bool]:
    """Map one ``env``/``headers`` dict to its external-agent form.

    ``${VAR}`` refs pass through; credential-like literal values become
    ``${KEY}`` references; other literals pass through. Returns the mapped
    dict and whether any value was redacted.
    """
    out: dict[str, Any] = {}
    redacted = False
    for key, raw_value in mapping.items():
        if raw_value is None or isinstance(raw_value, (dict, list)):
            continue
        value = str(raw_value)
        if "${" in value:
            out[key] = value
            continue
        if _CREDENTIAL_KEY_RE.search(key):
            out[key] = f"${{{key}}}"
            redacted = True
            manual.append(
                {
                    "id": f"manual:mcp-server-{kind}-secret:{name}",
                    "source": f"{source}.{kind}.{key}",
                    "message": f'Hermes MCP server "{name}" stores a credential under "{key}"; the export carries only a ${{{key}}} reference.',
                    "recommendation": f"Set {key} in the external-agent runtime environment before starting the gateway.",
                }
            )
        else:
            out[key] = value
    return out, redacted


def _resolve_connection_fields(
    raw: dict[str, Any], env: dict[str, str], name: str, source: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Connection fields (command/args/url/env/headers) plus ``${VAR}`` resolution.

    An ``env``/``headers`` value that references a missing var is a manual
    item and the field is dropped; dicts go through credential redaction.
    """
    manual: list[dict[str, str]] = []
    resolved: dict[str, Any] = {}
    for key in (*MCP_CONNECTION_FIELDS, "env", "headers"):
        value = raw.get(key)
        if value is None:
            continue
        if key in ("env", "headers") and isinstance(value, dict):
            mapped, _redacted = _redact_env_mapping(
                value, kind=key, name=name, manual=manual, source=source
            )
            if mapped:
                resolved[key] = mapped
            continue
        if isinstance(value, str) and "${" in value:
            fixed, unresolved = resolve_env_references(value, env)
            if unresolved:
                manual.append(
                    {
                        "id": f"manual:mcp-server-env-unresolved:{name}",
                        "source": f"{source}.{key}",
                        "message": f'Hermes MCP server "{name}" references an environment value that was not found in the Hermes .env file.',
                        "recommendation": "Define the missing value in the external agent's MCP server environment or headers manually.",
                    }
                )
                continue
            resolved[key] = fixed
        else:
            resolved[key] = value
    return resolved, manual


def _resolve_transport(raw: dict[str, Any], resolved: dict[str, Any]) -> Optional[str]:
    """The external-agent transport for a Hermes ``transport``/``type`` value."""
    transport = _opt_str(raw.get("transport")) or _opt_str(raw.get("type"))
    if transport in ("http", "streamable-http"):
        resolved["transport"] = "streamable-http"
    elif transport in ("sse", "stdio"):
        resolved["transport"] = transport
    elif not transport and _opt_str(resolved.get("url")):
        resolved["transport"] = "streamable-http"
    return transport


def _resolve_scalar_fields(raw: dict[str, Any], resolved: dict[str, Any]) -> None:
    """Timeouts (seconds -> ms) and boolean flags, camelCase or snake_case."""
    for key, seconds in (
        ("connectionTimeoutMs", raw.get("connectTimeout", raw.get("connect_timeout"))),
        ("requestTimeoutMs", raw.get("timeout")),
    ):
        if key not in resolved:
            seconds_value = _positive_number(seconds)
            if seconds_value is not None and seconds_value * 1000 < float("inf"):
                resolved[key] = int(seconds_value * 1000)
    for key, raw_key in (
        (
            "supportsParallelToolCalls",
            ("supportsParallelToolCalls", "supports_parallel_tool_calls"),
        ),
        ("sslVerify", ("sslVerify", "ssl_verify")),
    ):
        found = None
        for candidate in raw_key:
            if raw.get(candidate) is not None:
                found = _as_bool(raw.get(candidate))
                break
        if found is not None:
            resolved[key] = found


def _resolve_auth(
    raw: dict[str, Any], resolved: dict[str, Any]
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Auth mode plus OAuth metadata and client certificate paths."""
    auth = _opt_str(raw.get("auth"))
    resolved["auth"] = "oauth" if auth == "oauth" else None
    oauth = _map_oauth(raw)
    if oauth:
        resolved["oauth"] = oauth
    resolved.update(_map_client_certificate(raw))
    return auth, oauth


def _resolve_tool_filter(
    raw: dict[str, Any], resolved: dict[str, Any]
) -> tuple[Optional[list[str]], Optional[list[str]]]:
    """The external-agent ``toolFilter`` for a Hermes ``tools``/``toolFilter`` policy.

    Returns ``(include, exclude)`` as Hermes stated them (before fnmatch
    translation), for the manual-item notes below.
    """
    tools = _record(raw.get("tools"))
    include = _tool_filter_list(tools.get("include"))
    exclude = _tool_filter_list(tools.get("exclude"))
    resources = _as_bool(tools.get("resources"))
    prompts = _as_bool(tools.get("prompts"))
    direct = _record(raw.get("toolFilter")) or _record(raw.get("tool_filter"))
    if direct:
        direct_include = _tool_filter_list(direct.get("include"))
        direct_exclude = _tool_filter_list(direct.get("exclude"))
        if direct_include:
            resolved["toolFilter"] = {"include": direct_include}
        elif direct_exclude:
            resolved["toolFilter"] = {"exclude": direct_exclude}
    elif tools:
        if include is not None:
            allowed = sorted(p for p in include if not _has_unsupported_pattern(p))
            if resources is not False:
                allowed.extend(_RESOURCE_TOOLS)
            if prompts is not False:
                allowed.extend(_PROMPT_TOOLS)
            # Hermes' empty include disables native tools; the external agent's is unrestricted.
            resolved["toolFilter"] = (
                {"include": allowed} if allowed else {"exclude": ["*"]}
            )
        else:
            denied = list(exclude or [])
            if resources is False:
                denied.extend(_RESOURCE_TOOLS)
            if prompts is False:
                denied.extend(_PROMPT_TOOLS)
            if denied:
                resolved["toolFilter"] = {"exclude": denied}
    if (
        include is None
        and exclude
        and any(_has_unsupported_pattern(p) for p in exclude)
    ):
        # An untranslated exclusion would expose tools Hermes withheld.
        resolved["enabled"] = False
    return include, exclude


def _manual_item(
    suffix: str, source: str, name: str, message: str, recommendation: str
) -> dict[str, str]:
    """One manual item; the id slug is the server name, as in the export."""
    safe = "".join(
        ch if ch.isalnum() or ch in "-_." else "-" for ch in name.strip().lower()
    ).strip("-")
    return {
        "id": f"manual:mcp-server-{suffix}:{safe}",
        "source": source,
        "message": message,
        "recommendation": recommendation,
    }


def _manual_tls_items(
    name: str, raw: dict[str, Any], source: str, mapped: dict[str, Any]
) -> list[dict[str, str]]:
    """Manual items for client-certificate and TLS shapes the external agent cannot carry."""
    items: list[dict[str, str]] = []
    if (
        isinstance(raw.get("clientCert", raw.get("client_cert")), list)
        and len(raw.get("clientCert", raw.get("client_cert"))) == 3
    ):
        items.append(
            _manual_item(
                "client-cert-password",
                source,
                name,
                f'Hermes MCP server "{name}" uses a password-protected client key, which the external agent cannot represent in MCP config.',
                "Configure an unencrypted protected key path or an equivalent TLS proxy manually.",
            )
        )
    elif (
        raw.get("clientCert") is not None
        or raw.get("client_cert") is not None
        or raw.get("clientKey") is not None
        or raw.get("client_key") is not None
    ) and "clientCert" not in mapped:
        items.append(
            _manual_item(
                "client-cert",
                source,
                name,
                f'Hermes MCP server "{name}" uses a combined or invalid client-certificate shape that was not exported.',
                "Configure separate external-agent clientCert and clientKey file paths manually.",
            )
        )
    if isinstance(raw.get("sslVerify", raw.get("ssl_verify")), str):
        items.append(
            _manual_item(
                "tls-ca",
                source,
                name,
                f'Hermes MCP server "{name}" uses a CA bundle path for TLS verification, which external-agent MCP config cannot represent.',
                "Install the CA in the host trust store or configure an equivalent TLS proxy manually.",
            )
        )
    return items


def _manual_transport_auth_items(
    name: str,
    raw: dict[str, Any],
    source: str,
    transport: Optional[str],
    auth: Optional[str],
    oauth: Optional[dict[str, Any]],
) -> list[dict[str, str]]:
    """Manual items for transports, auth modes, and OAuth the external agent cannot carry."""
    items: list[dict[str, str]] = []
    if transport and transport not in ("http", "streamable-http", "sse", "stdio"):
        items.append(
            _manual_item(
                "transport",
                source,
                name,
                f'Hermes MCP server "{name}" uses unsupported transport "{transport}".',
                "Configure an equivalent external-agent MCP transport manually.",
            )
        )
    if auth and auth != "oauth":
        items.append(
            _manual_item(
                "auth",
                source,
                name,
                f'Hermes MCP server "{name}" uses unsupported authentication mode "{auth}".',
                "Configure an equivalent external-agent MCP authentication mode manually.",
            )
        )
    if auth == "oauth" or oauth:
        items.append(
            _manual_item(
                "oauth-login",
                source,
                name,
                f'Hermes MCP server "{name}" requires OAuth login in the external agent.',
                f'Run "openclaw mcp login {name}" after migration.',
            )
        )
    if oauth and any(
        k not in ("authProfileId", "scope", "redirectUrl", "clientMetadataUrl")
        for k in oauth
    ):
        items.append(
            _manual_item(
                "oauth-client",
                source,
                name,
                f'Hermes MCP server "{name}" uses pre-registered OAuth client settings that were not copied into the export.',
                f'Run "openclaw mcp login {name}" and configure supported OAuth metadata manually.',
            )
        )
    return items


def _manual_tool_items(
    name: str,
    raw: dict[str, Any],
    source: str,
    include: Optional[list[str]],
    exclude: Optional[list[str]],
) -> list[dict[str, str]]:
    """Manual items for tool patterns and policies that do not translate exactly."""
    items: list[dict[str, str]] = []
    active_patterns = include if include is not None else exclude
    if active_patterns and any(_has_unsupported_pattern(p) for p in active_patterns):
        items.append(
            _manual_item(
                "tool-patterns",
                source,
                name,
                f'Hermes MCP server "{name}" has tool patterns that were omitted because the external agent supports only exact names and "*".'
                if include is not None
                else f'Hermes MCP server "{name}" was exported disabled because its tool exclusions use unsupported fnmatch patterns.',
                "Replace ? and bracket patterns with exact tool names or equivalent * patterns in mcp.servers toolFilter, then enable the server if disabled.",
            )
        )
    tools = _record(raw.get("tools"))
    resources = _as_bool(tools.get("resources"))
    prompts = _as_bool(tools.get("prompts"))
    if tools and (
        any(k not in ("include", "exclude", "resources", "prompts") for k in tools)
        or (tools.get("include") is not None and include is None)
        or (tools.get("exclude") is not None and exclude is None)
        or (tools.get("resources") is not None and resources is None)
        or (tools.get("prompts") is not None and prompts is None)
    ):
        items.append(
            _manual_item(
                "tool-policy",
                source,
                name,
                f'Hermes MCP server "{name}" has a tool policy that cannot be translated exactly.',
                "Review and configure mcp.servers toolFilter manually.",
            )
        )
    return items


def _manual_lifecycle_items(
    name: str, raw: dict[str, Any], source: str
) -> list[dict[str, str]]:
    """Manual items for lifecycle behaviours external-agent MCP config does not expose."""
    lifecycle = _record(raw.get("lifecycle"))
    unsupported = [
        ("preflight", raw.get("skip_preflight") is True),
        (
            "sampling",
            isinstance(raw.get("sampling"), dict)
            and raw["sampling"].get("enabled") is not False,
        ),
        (
            "elicitation",
            isinstance(raw.get("elicitation"), dict)
            and raw["elicitation"].get("enabled") is not False,
        ),
        (
            "lifecycle",
            _positive_number(
                raw.get("idle_timeout_seconds", lifecycle.get("idle_timeout_seconds"))
            )
            is not None
            or _positive_number(
                raw.get("max_lifetime_seconds", lifecycle.get("max_lifetime_seconds"))
            )
            is not None,
        ),
        ("keepalive", _positive_number(raw.get("keepalive_interval")) is not None),
    ]
    items: list[dict[str, str]] = []
    for feature, configured in unsupported:
        if configured:
            items.append(
                _manual_item(
                    feature,
                    source,
                    name,
                    f'Hermes MCP server "{name}" uses {feature} behavior that external-agent MCP config does not expose.',
                    "Review the server requirement and configure an equivalent deployment or runtime policy manually.",
                )
            )
    return items


def map_hermes_mcp_server(
    name: str, raw: dict[str, Any], env: dict[str, str], source: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Map one Hermes MCP server entry to its external-agent ``mcp.servers`` form.

    Returns ``(server, manual_items)``; an empty ``server`` means the entry
    carries nothing the external agent can represent (no command and no URL).
    """
    manual: list[dict[str, str]] = []
    resolved, field_manual = _resolve_connection_fields(raw, env, name, source)
    manual.extend(field_manual)
    transport = _resolve_transport(raw, resolved)
    _resolve_scalar_fields(raw, resolved)
    auth, oauth = _resolve_auth(raw, resolved)
    include, exclude = _resolve_tool_filter(raw, resolved)
    resolved["env"] = resolved.get("env")
    resolved["headers"] = resolved.get("headers")
    mapped = {k: v for k, v in resolved.items() if v is not None}
    manual.extend(_manual_tls_items(name, raw, source, mapped))
    manual.extend(
        _manual_transport_auth_items(name, raw, source, transport, auth, oauth)
    )
    manual.extend(_manual_tool_items(name, raw, source, include, exclude))
    manual.extend(_manual_lifecycle_items(name, raw, source))
    if not (_opt_str(mapped.get("command")) or _opt_str(mapped.get("url"))):
        return {}, manual
    return mapped, manual


def collect_mcp_servers(
    config: dict[str, Any], env: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """All Hermes MCP servers as external-agent ``mcp.servers`` entries.

    Reads ``mcp_servers`` first, then ``mcp.servers``, then a bare ``mcp``
    mapping — the same precedence the forward provider uses.
    """
    mcp = config.get("mcp")
    if config.get("mcp_servers") is not None:
        raw_servers, source = config.get("mcp_servers"), "config.yaml:mcp_servers"
    elif isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        raw_servers, source = mcp["servers"], "config.yaml:mcp.servers"
    else:
        raw_servers, source = mcp, "config.yaml:mcp"
    servers: dict[str, dict[str, Any]] = {}
    manual: list[dict[str, str]] = []
    if not isinstance(raw_servers, dict):
        return servers, manual
    for name, raw_server in raw_servers.items():
        if not isinstance(raw_server, dict):
            continue
        mapped, items = map_hermes_mcp_server(name, raw_server, env, f"{source}.{name}")
        manual.extend(items)
        if mapped:
            servers[name] = mapped
    return servers, manual
