"""Model + provider mapping for the reverse migration (Hermes -> the external agent).

Ports the read half of the external agent's ``extensions/migrate-hermes`` — its
``model.ts``, ``config-providers.ts`` and ``config-provider-contract.ts`` —
and emits the external agent side instead of consuming it: Hermes
``config.yaml`` model/providers become external-agent ``models.providers.*``
entries (the ``providerConfig()`` row shape) plus the
``agents.defaults.model`` ref. Model rows are enriched from Hermes's own
bundled seed catalog (``agent/model_catalog_seed.json``) when the
provider+model matches; unknown models keep only what Hermes states —
context window / token caps are omitted, never invented, and the external agent's
catalog merge fills them in.

Secrets: an API key is never emitted as a value. When a key env var is
known, the entry carries ``apiKey: "${VAR}"`` — the env-template form
the external agent resolves at runtime — and the var is listed in ``.env.example``
and the report. An inline key in ``config.yaml`` is reported as a manual
item and never copied.
"""

from __future__ import annotations

import math
from typing import Any, Optional
from urllib.parse import urlparse

# --- Provider id normalization (verbatim data port of forward model.ts) ---

PROVIDER_ALIASES = {
    "alibaba": "qwen",
    "alibaba-cloud": "qwen",
    "alibaba-coding": "qwen",
    "alibaba-coding-plan": "qwen",
    "alibaba_coding": "qwen",
    "alibaba_coding_plan": "qwen",
    "aliyun": "qwen",
    "azure-foundry": "microsoft-foundry",
    "bedrock": "amazon-bedrock",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "copilot": "github-copilot",
    "gemini": "google",
    "github": "github-copilot",
    "github-model": "github-copilot",
    "github-models": "github-copilot",
    "glm": "zai",
    "google-ai-studio": "google",
    "google-gemini": "google",
    "grok": "xai",
    "kilo": "kilocode",
    "kilo-code": "kilocode",
    "kilo-gateway": "kilocode",
    "kimi-cn": "moonshot",
    "kimi-for-coding": "kimi",
    "kimi-coding": "kimi",
    "kimi-coding-cn": "moonshot",
    "moonshot-cn": "moonshot",
    "minimax-global": "minimax-portal",
    "minimax-cn": "minimax",
    "minimax-oauth": "minimax-portal",
    "minimax_oauth": "minimax-portal",
    "opencode-zen": "opencode",
    "openai-api": "openai",
    "openai-codex": "openai",
    "dashscope": "qwen",
    "qwen-cli": "qwen",
    "qwen-oauth": "qwen",
    "qwen-portal": "qwen",
    "x-ai": "xai",
    "x-ai-oauth": "xai",
    "x.ai": "xai",
    "xai-grok-oauth": "xai",
    "xai-oauth": "xai",
    "grok-oauth": "xai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zen": "opencode",
    "zhipu": "zai",
    "vertex": "google-vertex",
}

CANONICAL_PROVIDER_IDS = {
    "alibaba",
    "alibaba-coding-plan",
    "anthropic",
    "arcee",
    "azure-foundry",
    "bedrock",
    "copilot",
    "copilot-acp",
    "deepseek",
    "fireworks",
    "github-copilot",
    "gemini",
    "gmi",
    "huggingface",
    "kilo",
    "kilocode",
    "kimi-coding",
    "kimi-coding-cn",
    "kimi-for-coding",
    "lmstudio",
    "minimax",
    "minimax-cn",
    "minimax-oauth",
    "moa",
    "nous",
    "novita",
    "nvidia",
    "ollama-cloud",
    "openai-api",
    "openai-codex",
    "opencode",
    "opencode-go",
    "opencode-zen",
    "openrouter",
    "stepfun",
    "tencent-tokenhub",
    "xai",
    "xai-oauth",
    "xiaomi",
    "zai",
    "vertex",
}

RETIRED_QWEN_PROVIDER_IDS = {"qwen-cli", "qwen-oauth", "qwen-portal"}

DYNAMIC_KIMI_PROVIDER_IDS = {"kimi", "kimi-coding", "kimi-for-coding", "moonshot"}

# Hermes transport names -> external-agent model api (verbatim port of the
# forward contract's HERMES_TRANSPORTS).
TRANSPORTS = {
    "anthropic_messages": "anthropic-messages",
    "chat_completions": "openai-completions",
    "codex_responses": "openai-responses",
    "openai_chat": "openai-completions",
}

TRANSPORT_ALIASES = {
    "openai": "chat_completions",
    "openai-chat": "chat_completions",
    "chat-completions": "chat_completions",
    "chatcompletions": "chat_completions",
    "responses": "codex_responses",
    "openai_responses": "codex_responses",
    "openai-responses": "codex_responses",
    "anthropic": "anthropic_messages",
    "anthropic-messages": "anthropic_messages",
    "messages": "anthropic_messages",
}

# Provider -> (base-url env vars, api-key env var). Verbatim port of the
# forward contract's HERMES_PROVIDER_ENV + HERMES_SPECIAL_PROVIDER_ENV.
PROVIDER_ENV: dict[str, dict[str, Any]] = {
    "custom": {
        "base_url_env": ["CUSTOM_BASE_URL", "OPENAI_BASE_URL"],
        "api_key_env": "OPENAI_API_KEY",
    },
    "openai-api": {
        "base_url_env": ["OPENAI_BASE_URL"],
        "api_key_env": "OPENAI_API_KEY",
    },
    "xai-oauth": {"base_url_env": ["HERMES_XAI_BASE_URL", "XAI_BASE_URL"]},
    "qwen-oauth": {"base_url_env": ["HERMES_QWEN_BASE_URL"]},
    "qwen-cli": {"base_url_env": ["HERMES_QWEN_BASE_URL"]},
    "qwen-portal": {"base_url_env": ["HERMES_QWEN_BASE_URL"]},
    "minimax-cn": {
        "base_url_env": ["MINIMAX_CN_BASE_URL"],
        "api_key_env": "MINIMAX_CN_API_KEY",
    },
    "alibaba-coding-plan": {
        "base_url_env": ["ALIBABA_CODING_PLAN_BASE_URL"],
        "api_key_env": "ALIBABA_CODING_PLAN_API_KEY",
    },
    "anthropic": {
        "base_url_env": ["ANTHROPIC_BASE_URL"],
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "arcee": {"base_url_env": ["ARCEE_BASE_URL"], "api_key_env": "ARCEEAI_API_KEY"},
    "azure-foundry": {"base_url_env": ["AZURE_FOUNDRY_BASE_URL"]},
    "deepseek": {
        "base_url_env": ["DEEPSEEK_BASE_URL"],
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "gmi": {"base_url_env": ["GMI_BASE_URL"]},
    "google": {"base_url_env": ["GEMINI_BASE_URL"], "api_key_env": "GOOGLE_API_KEY"},
    "huggingface": {"base_url_env": ["HF_BASE_URL"], "api_key_env": "HF_TOKEN"},
    "kilocode": {
        "base_url_env": ["KILOCODE_BASE_URL"],
        "api_key_env": "KILOCODE_API_KEY",
    },
    "kimi": {"base_url_env": ["KIMI_BASE_URL"], "api_key_env": "KIMI_API_KEY"},
    "lmstudio": {"base_url_env": ["LM_BASE_URL"], "api_key_env": "LM_API_KEY"},
    "minimax": {"base_url_env": ["MINIMAX_BASE_URL"], "api_key_env": "MINIMAX_API_KEY"},
    "novita": {"base_url_env": ["NOVITA_BASE_URL"]},
    "nvidia": {"base_url_env": ["NVIDIA_BASE_URL"], "api_key_env": "NVIDIA_API_KEY"},
    "ollama-cloud": {"base_url_env": ["OLLAMA_BASE_URL"]},
    "opencode": {
        "base_url_env": ["OPENCODE_ZEN_BASE_URL"],
        "api_key_env": "OPENCODE_ZEN_API_KEY",
    },
    "opencode-go": {
        "base_url_env": ["OPENCODE_GO_BASE_URL"],
        "api_key_env": "OPENCODE_GO_API_KEY",
    },
    "openrouter": {
        "base_url_env": ["OPENROUTER_BASE_URL"],
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "qwen": {
        "base_url_env": ["DASHSCOPE_BASE_URL"],
        "api_key_env": "DASHSCOPE_API_KEY",
    },
    "stepfun": {"base_url_env": ["STEPFUN_BASE_URL"], "api_key_env": "STEPFUN_API_KEY"},
    "tencent-tokenhub": {"base_url_env": ["TOKENHUB_BASE_URL"]},
    "xai": {"base_url_env": ["XAI_BASE_URL"], "api_key_env": "XAI_API_KEY"},
    "xiaomi": {"base_url_env": ["XIAOMI_BASE_URL"], "api_key_env": "XIAOMI_API_KEY"},
    "zai": {"base_url_env": ["GLM_BASE_URL"], "api_key_env": "ZAI_API_KEY"},
}

_IMPLICIT_BASE_URLS = {
    (
        "alibaba",
        "alibaba-cloud",
        "aliyun",
        "dashscope",
    ): "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ("kimi-coding-cn", "kimi-cn", "moonshot-cn"): "https://api.moonshot.cn/v1",
    ("minimax-cn", "minimax-china", "minimax_cn"): "https://api.minimaxi.com/anthropic",
}


def _opt_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_custom_provider_id(provider: str) -> str:
    """Lowercase, strip a ``custom:`` prefix, spaces become dashes."""
    normalized = provider.strip().lower()
    normalized = normalized.removeprefix("custom:")
    return normalized.replace(" ", "-")


def normalize_provider_id(provider: str) -> str:
    """Canonical external-agent provider id for a Hermes provider name."""
    normalized = normalize_custom_provider_id(provider)
    return PROVIDER_ALIASES.get(normalized, normalized)


def _is_retired_qwen(value: str) -> bool:
    slash = value.find("/")
    provider = value[:slash] if slash > 0 else value
    return normalize_custom_provider_id(provider) in RETIRED_QWEN_PROVIDER_IDS


def uses_retired_qwen_provider(config: dict[str, Any]) -> bool:
    """Whether any model ref in the config names a retired Qwen provider."""
    model = _record(config.get("model"))
    candidates = [
        _opt_str(config.get("provider")),
        config.get("model") if isinstance(config.get("model"), str) else None,
        _opt_str(model.get("provider")),
        _opt_str(model.get("default")),
        _opt_str(model.get("model")),
        _opt_str(config.get("default_model")),
        _opt_str(config.get("model_name")),
    ]
    return any(c is not None and _is_retired_qwen(c) for c in candidates)


def _read_base_url(value: dict[str, Any]) -> Optional[str]:
    for key in ("base_url", "baseUrl", "url", "api"):
        found = _opt_str(value.get(key))
        if found:
            return found
    return None


def _resolve_kimi_provider_id(
    config: dict[str, Any], provider: str, env: dict[str, str]
) -> Optional[str]:
    if normalize_custom_provider_id(provider) not in DYNAMIC_KIMI_PROVIDER_IDS:
        return None
    base_url = _read_kimi_base_url(config, provider, env)
    if base_url:
        try:
            parsed = urlparse(base_url)
            host = (parsed.hostname or "").lower()
            path = parsed.path.lower().rstrip("/")
            if host == "api.kimi.com" and path in ("/coding", "/coding/v1"):
                return "kimi"
            if host == "api.moonshot.ai":
                return "moonshot"
        except ValueError:
            pass
        return "moonshot" if normalize_provider_id(provider) == "moonshot" else "kimi"
    api_key = (env.get("KIMI_API_KEY") or "").strip() or (
        env.get("KIMI_CODING_API_KEY") or ""
    ).strip()
    return "kimi" if api_key.startswith("sk-kimi-") else "moonshot"


def _read_kimi_base_url(
    config: dict[str, Any], provider: str, env: dict[str, str]
) -> Optional[str]:
    model = _record(config.get("model"))
    selected = _opt_str(model.get("provider")) or _opt_str(config.get("provider"))
    if selected and normalize_custom_provider_id(selected) in DYNAMIC_KIMI_PROVIDER_IDS:
        found = _read_base_url(model)
        if found:
            return found
    for pid, value in _record(config.get("providers")).items():
        if normalize_custom_provider_id(pid) == normalize_custom_provider_id(provider):
            found = _read_base_url(_record(value))
            if found:
                return found
    return (env.get("KIMI_BASE_URL") or "").strip() or None


def _has_explicit_provider(config: dict[str, Any], provider: str) -> bool:
    normalized = normalize_custom_provider_id(provider)
    if normalized in CANONICAL_PROVIDER_IDS or normalized in RETIRED_QWEN_PROVIDER_IDS:
        return False
    if any(
        normalize_custom_provider_id(pid) == normalized
        for pid in _record(config.get("providers"))
    ):
        return True
    custom = config.get("custom_providers")
    if isinstance(custom, list):
        for entry in custom:
            entry_id = _opt_str(_record(entry).get("name")) or _opt_str(
                _record(entry).get("id")
            )
            if entry_id and normalize_custom_provider_id(entry_id) == normalized:
                return True
    return False


def resolve_configured_provider_id(
    config: dict[str, Any], provider: str, env: dict[str, str]
) -> str:
    """External-agent provider id for a Hermes-configured provider name."""
    if _has_explicit_provider(config, provider):
        return normalize_custom_provider_id(provider)
    return _resolve_kimi_provider_id(config, provider, env) or normalize_provider_id(
        provider
    )


def _join_provider_model(
    config: dict[str, Any], provider: Optional[str], model: str, env: dict[str, str]
) -> str:
    if not provider:
        slash = model.find("/")
        if slash > 0 and _is_retired_qwen(model):
            return f"qwen/{model[slash + 1 :]}"
        return model
    if provider.strip().lower() == "auto":
        slash = model.find("/")
        if slash > 0:
            return f"{resolve_configured_provider_id(config, model[:slash], env)}/{model[slash + 1 :]}"
        return model
    explicit = _has_explicit_provider(config, provider)
    normalized = resolve_configured_provider_id(config, provider, env)
    slash = model.find("/")
    if slash > 0:
        model_provider = (
            normalize_custom_provider_id(model[:slash])
            if explicit
            else resolve_configured_provider_id(config, model[:slash], env)
        )
        if model_provider == normalized:
            return f"{normalized}/{model[slash + 1 :]}"
    return model if model.startswith(f"{normalized}/") else f"{normalized}/{model}"


def resolve_hermes_model_ref(
    config: dict[str, Any], env: dict[str, str] | None = None
) -> Optional[str]:
    """The ``provider/model`` ref for the Hermes default model, or None."""
    env = env or {}
    model = config.get("model")
    if isinstance(model, str) and model.strip():
        return _join_provider_model(
            config, _opt_str(config.get("provider")), model.strip(), env
        )
    if isinstance(model, dict):
        raw = _opt_str(model.get("default")) or _opt_str(model.get("model"))
        has_endpoint = bool(
            _opt_str(model.get("base_url")) or _opt_str(model.get("baseUrl"))
        )
        provider = _opt_str(model.get("provider")) or (
            "custom" if has_endpoint else None
        )
        return raw if not raw else _join_provider_model(config, provider, raw, env)
    root = _opt_str(config.get("default_model")) or _opt_str(config.get("model_name"))
    if root:
        return _join_provider_model(config, _opt_str(config.get("provider")), root, env)
    return None


def resolve_model_fallbacks(
    config: dict[str, Any], env: dict[str, str] | None = None
) -> list[str]:
    """Ordered fallback ``provider/model`` refs, if the config states any."""
    env = env or {}
    model = _record(config.get("model"))
    raw_list = model.get("fallbacks") or model.get("fallback_models") or []
    provider = _opt_str(model.get("provider")) or _opt_str(config.get("provider"))
    refs: list[str] = []
    if isinstance(raw_list, list):
        for entry in raw_list:
            text = _opt_str(entry)
            if text:
                refs.append(_join_provider_model(config, provider, text, env))
    return refs


# --- Env-reference helpers (port of forward config-env.ts, minimal) ---


def read_env_reference(value: Any) -> Optional[str]:
    """The var name when ``value`` is exactly a ``${VAR}`` reference."""
    raw = _opt_str(value)
    if raw and raw.startswith("${") and raw.endswith("}"):
        name = raw[2:-1].strip()
        return name or None
    return None


def resolve_env_references(
    value: str, env: dict[str, str]
) -> tuple[Optional[str], bool]:
    """Resolve ``${VAR}`` occurrences in ``value`` against ``env``.

    Returns ``(resolved, unresolved)``; ``unresolved`` is True when a
    referenced var is absent.
    """
    import re

    unresolved = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal unresolved
        name = match.group(1).strip()
        found = env.get(name)
        if found is None:
            unresolved = True
            return match.group(0)
        return found

    return re.sub(r"\$\{([^}]+)\}", _replace, value), unresolved


# --- Provider collection (port of forward config-providers.ts) ---


def _provider_env(provider_id: str) -> dict[str, Any]:
    source = normalize_custom_provider_id(provider_id)
    return PROVIDER_ENV.get(source) or PROVIDER_ENV.get(
        normalize_provider_id(source), {}
    )


def resolve_provider_base_url_env(
    provider_id: Optional[str], env: dict[str, str]
) -> Optional[str]:
    """A base URL implied by a provider env var (e.g. ``OPENROUTER_BASE_URL``)."""
    if not provider_id:
        return None
    for name in _provider_env(provider_id).get("base_url_env", []):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def resolve_provider_api_key_env(provider_id: Optional[str]) -> Optional[str]:
    """The env var that carries this provider's API key, if one is known."""
    if not provider_id:
        return None
    return _provider_env(provider_id).get("api_key_env")


def resolve_implicit_base_url(provider_id: Optional[str]) -> Optional[str]:
    """A well-known base URL for providers that Hermes leaves implicit."""
    if not provider_id:
        return None
    key = provider_id.strip().lower()
    for names, url in _IMPLICIT_BASE_URLS.items():
        if key in names:
            return url
    return None


def resolve_endpoint_api_key_env(base_url: str) -> Optional[str]:
    """Key env var implied by an OpenAI-hosted endpoint, if any."""
    host = (urlparse(base_url).hostname or "").lower()
    if (
        host == "openai.com"
        or host.endswith(".openai.com")
        or host == "openai.azure.com"
        or host.endswith(".openai.azure.com")
    ):
        return "OPENAI_API_KEY"
    return None


def _read_transport(raw: dict[str, Any]) -> Optional[str]:
    transport = (
        _opt_str(raw.get("api_mode"))
        or _opt_str(raw.get("apiMode"))
        or _opt_str(raw.get("transport"))
    )
    if not transport:
        return None
    lowered = transport.lower()
    return TRANSPORT_ALIASES.get(lowered, lowered)


def resolve_provider_api(
    raw: dict[str, Any], provider_id: Optional[str]
) -> Optional[str]:
    """External-agent model api (``openai-completions`` etc.) for a Hermes provider."""
    transport = _read_transport(raw)
    source = (provider_id or "").strip().lower()
    if source == "openai-codex":
        return "openai-chatgpt-responses"
    if transport and transport != "codex_responses":
        return TRANSPORTS.get(transport)
    provider = normalize_provider_id(source) if source else ""
    base_url = _read_base_url(raw) or ""
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().rstrip("/")
    if transport == "codex_responses" and source != "custom":
        return "openai-responses"
    if (
        provider in ("anthropic", "minimax", "minimax-cn", "minimax-oauth")
        or host == "api.anthropic.com"
        or (
            host == "api.kimi.com"
            and (path == "/coding" or path.startswith("/coding/"))
        )
        or path.endswith("/anthropic")
        or path.endswith("/anthropic/v1")
    ):
        return "anthropic-messages"
    if host == "chatgpt.com" and "/backend-api/codex" in path:
        return "openai-chatgpt-responses"
    if source == "openai-api":
        return "openai-responses"
    if transport == "codex_responses":
        return "openai-responses"
    if provider == "xai" or host in ("api.x.ai", "api.openai.com"):
        return "openai-responses"
    return "openai-completions"


def _read_provider_base_url(
    raw: dict[str, Any], env: dict[str, str]
) -> tuple[Optional[str], bool, bool]:
    """``(base_url, sensitive, unresolved)`` for one provider entry."""
    value = _read_base_url(raw)
    if not value:
        return None, False, False
    resolved, unresolved = resolve_env_references(value, env)
    referenced = "${" in value
    return (None if unresolved else resolved), referenced, unresolved


def _read_api_key_env(raw: dict[str, Any]) -> Optional[str]:
    for key in ("key_env", "api_key_env", "keyEnv", "apiKeyEnv", "env"):
        found = _opt_str(raw.get(key))
        if found:
            return found
    return read_env_reference(raw.get("api_key") or raw.get("apiKey"))


def _read_model_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    def _positive(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return int(value)
        return None

    out: dict[str, Any] = {}
    for key in ("context_length", "contextLength", "contextWindow"):
        found = _positive(raw.get(key))
        if found is not None:
            out["contextWindow"] = found
            break
    for key in ("max_tokens", "max_output_tokens", "maxTokens"):
        found = _positive(raw.get(key))
        if found is not None:
            out["maxTokens"] = found
            break
    vision = raw.get("supports_vision", raw.get("supportsVision"))
    if isinstance(vision, bool):
        out["supportsVision"] = vision
    return out


def collect_provider_models(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Model rows declared on one provider entry (port of the forward contract)."""
    models: dict[str, dict[str, Any]] = {}
    root_meta = _read_model_metadata(raw)
    entries: list[tuple[Optional[str], Any]] = []
    if isinstance(raw.get("models"), list):
        for model in raw["models"]:
            if isinstance(model, dict):
                entries.append(
                    (_opt_str(model.get("id")) or _opt_str(model.get("name")), model)
                )
            else:
                entries.append((_opt_str(model), {}))
    elif isinstance(raw.get("models"), dict):
        for model_id, meta in raw["models"].items():
            if model_id in (
                "__discovered_model_catalog__",
                "__explicit_model_allowlist__",
            ):
                continue
            entries.append((model_id, meta))
    for model_id, meta in entries:
        if not model_id:
            continue
        merged = {
            **root_meta,
            **(_read_model_metadata(meta) if isinstance(meta, dict) else {}),
        }
        models[model_id] = {"id": model_id, **merged}
    for fallback in (
        _opt_str(raw.get("default_model")) or _opt_str(raw.get("defaultModel")),
        _opt_str(raw.get("default")),
        _opt_str(raw.get("model")),
    ):
        if fallback and fallback not in models:
            models[fallback] = {"id": fallback, **root_meta}
    return list(models.values())


class ProviderEntry:
    """One collected Hermes provider, ready to emit as external-agent config."""

    def __init__(
        self,
        provider_id: str,
        base_url: str,
        api: str,
        api_key_env: Optional[str],
        models: list[dict[str, Any]],
        headers: Optional[dict[str, Any]] = None,
        sensitive: bool = False,
    ):
        self.id = provider_id
        self.base_url = base_url
        self.api = api
        self.api_key_env = api_key_env
        self.models = models
        self.headers = headers
        self.sensitive = sensitive


def _provider_sources(config: dict[str, Any]):
    for pid, raw in _record(config.get("providers")).items():
        if isinstance(raw, dict):
            yield pid, raw, f"config.yaml:providers.{pid}", False
    custom = config.get("custom_providers")
    if isinstance(custom, list):
        for raw in custom:
            if not isinstance(raw, dict):
                continue
            pid = _opt_str(raw.get("name")) or _opt_str(raw.get("id"))
            if pid:
                yield pid, raw, f"config.yaml:custom_providers.{pid}", True


def collect_hermes_providers(
    config: dict[str, Any], env: dict[str, str] | None = None
) -> tuple[list[ProviderEntry], list[dict[str, str]]]:
    """Collect every Hermes provider entry.

    Returns ``(providers, manual_items)``; manual items name providers that
    could not be emitted safely (unresolved endpoint, inline key, unsupported
    transport) with what to do about them.
    """
    env = env or {}
    collected: list[ProviderEntry] = []
    manual: list[dict[str, str]] = []

    def upsert(entry: ProviderEntry, *, fallback_only: bool = False) -> None:
        for index, existing in enumerate(collected):
            if existing.id != entry.id:
                continue
            merged_models = [*existing.models]
            for model in entry.models:
                if not any(m["id"] == model["id"] for m in merged_models):
                    merged_models.append(model)
            base = entry if fallback_only else existing
            override = existing if fallback_only else entry
            collected[index] = ProviderEntry(
                base.id,
                override.base_url,
                override.api,
                override.api_key_env or base.api_key_env,
                merged_models,
                override.headers or base.headers,
                base.sensitive or override.sensitive,
            )
            return
        collected.append(entry)

    for pid, raw, source, custom in _provider_sources(config):
        base_url, sensitive, unresolved = _read_provider_base_url(raw, env)
        resolved = base_url or (None if custom else resolve_implicit_base_url(pid))
        if resolved is None:
            resolved = resolve_provider_base_url_env(pid, env)
        api = resolve_provider_api(
            {**raw, "base_url": resolved} if resolved else raw, pid
        )
        if not resolved or not api:
            if unresolved:
                manual.append(
                    {
                        "id": f"manual:model-provider-endpoint-env:{pid}",
                        "source": source,
                        "message": f'Hermes provider "{pid}" references an endpoint environment variable that was not present in the Hermes .env file.',
                        "recommendation": "Configure the provider endpoint manually after migration.",
                    }
                )
            else:
                manual.append(
                    {
                        "id": f"manual:model-provider-endpoint:{pid}",
                        "source": source,
                        "message": f'Hermes provider "{pid}" has no explicit endpoint to export safely.',
                        "recommendation": "Configure the provider endpoint manually after migration.",
                    }
                )
            continue
        transport = _read_transport(raw)
        if transport and transport not in TRANSPORTS:
            manual.append(
                {
                    "id": f"manual:model-provider-transport:{pid}",
                    "source": f"{source}.transport",
                    "message": f'Hermes provider "{pid}" uses unsupported transport "{transport}".',
                    "recommendation": "Configure an equivalent external-agent provider plugin or API adapter manually.",
                }
            )
            continue
        inline_key = raw.get("api_key") or raw.get("apiKey")
        if _opt_str(inline_key) and not read_env_reference(inline_key):
            manual.append(
                {
                    "id": f"manual:model-provider-inline-key:{pid}",
                    "source": f"{source}.api_key",
                    "message": f'Hermes provider "{pid}" contains an inline API key that was not copied into the export.',
                    "recommendation": "Move the key to an environment variable or external-agent secret provider.",
                }
            )
        key_env = (
            _read_api_key_env(raw)
            or resolve_provider_api_key_env(pid)
            or resolve_endpoint_api_key_env(resolved)
        )
        headers = _read_headers(raw, env)
        upsert(
            ProviderEntry(
                resolve_configured_provider_id(config, pid, env),
                resolved,
                api,
                key_env,
                collect_provider_models(raw),
                headers,
                sensitive,
            ),
            fallback_only=custom,
        )

    # The selected model/provider is itself a provider when it names an endpoint.
    model = _record(config.get("model"))
    raw_provider = (
        _opt_str(model.get("provider")) if model else _opt_str(config.get("provider"))
    )
    raw = model if model else {}
    base_url, sensitive, unresolved = _read_provider_base_url(raw, env)
    env_base = resolve_provider_base_url_env(raw_provider, env)
    resolved = base_url or env_base or resolve_implicit_base_url(raw_provider)
    api = resolve_provider_api(
        {**raw, "base_url": resolved} if resolved else raw, raw_provider
    )
    if (model or raw_provider) and resolved and api:
        key_env = (
            _read_api_key_env(raw)
            or resolve_provider_api_key_env(raw_provider)
            or resolve_endpoint_api_key_env(resolved)
        )
        upsert(
            ProviderEntry(
                resolve_configured_provider_id(config, raw_provider, env)
                if raw_provider
                else "custom",
                resolved,
                api,
                key_env,
                collect_provider_models(raw),
                _read_headers(raw, env) if model else None,
                (not model) or sensitive or bool(env_base),
            )
        )
    return collected, manual


def _read_headers(raw: dict[str, Any], env: dict[str, str]) -> Optional[dict[str, Any]]:
    """Extra request headers, with ``${VAR}`` refs resolved from the .env.

    Header values that stay unresolved are dropped (the provider manual
    items cover them); header values are never secret values — a literal
    that matches a harvested secret is caught by the post-write scan.
    """
    source = raw.get("extra_headers")
    if not isinstance(source, dict) or not source:
        return None
    headers: dict[str, Any] = {}
    for name, value in source.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        resolved, unresolved = resolve_env_references(str(value), env)
        if unresolved:
            continue
        headers[name] = resolved
    return headers or None


def _normalize_base_url(base_url: str, api: str) -> str:
    """Canonical base URL: Anthropic endpoints drop a trailing ``/v1`` (the
    SDK appends ``/v1/messages`` itself). Port of the forward contract."""
    if api != "anthropic-messages":
        return base_url
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return base_url
    path = parsed.path
    if path.endswith("/v1"):
        path = path[:-3]
    elif path.endswith("/v1/"):
        path = path[:-4]
    rebuilt = parsed._replace(path=path.rstrip("/") or "/")
    return rebuilt.geturl().rstrip("/")


def _bundled_seed_row(provider_id: str, model_id: str) -> Optional[dict[str, Any]]:
    """One raw row from the bundled seed catalog, or None.

    Matches exact id first, then case-insensitive, then with dots as
    dashes (Hermes ids use dots, seed catalog ids use dashes).
    """
    try:
        from agent.model_catalog_data import (
            load_bundled_seed_catalog,
            normalize_provider_id,
        )
    except ImportError:
        return None
    catalog = load_bundled_seed_catalog()
    wanted_provider = normalize_provider_id(provider_id)
    section: Optional[dict[str, Any]] = None
    for key, value in catalog.items():
        if normalize_provider_id(key) == wanted_provider and isinstance(value, dict):
            section = value
            break
    if not section or not isinstance(section.get("models"), list):
        return None
    models = section["models"]
    for candidate in (model_id, model_id.replace(".", "-")):
        for row in models:
            if isinstance(row, dict) and row.get("id") == candidate:
                return row
        lowered = candidate.lower()
        for row in models:
            if (
                isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and row["id"].lower() == lowered
            ):
                return row
    return None


def enrich_model_row(provider_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Fill a model row from the bundled seed catalog when it matches.

    Only catalog facts are copied (context window, token cap, cost, input
    modalities); the Hermes-declared id always wins.
    """
    try:
        from agent.model_catalog_data import catalog_cost_to_seed_shape
    except ImportError:
        return dict(row)
    catalog_row = _bundled_seed_row(provider_id, row["id"])
    if not catalog_row:
        return dict(row)
    enriched = dict(row)
    for key in ("contextWindow", "maxTokens", "reasoning", "input"):
        if key in catalog_row and key not in enriched:
            enriched[key] = catalog_row[key]
    cost = catalog_cost_to_seed_shape(catalog_row.get("cost"))
    if cost is not None and "cost" not in enriched:
        enriched["cost"] = cost
    return enriched


def provider_config_entry(entry: ProviderEntry) -> dict[str, Any]:
    """The external-agent ``models.providers.<id>`` entry for one provider.

    Same row shape as the forward contract's ``providerConfig()``:
    ``baseUrl``, ``api``, optional ``apiKey`` as an env template, and
    ``models`` rows with ``id``/``name``/``api``/``reasoning``/``input``/
    ``cost``/``contextWindow``/``maxTokens``/``baseUrl``/``metadataSource``.
    """
    models = entry.models or [{"id": "default"}]
    base_url = _normalize_base_url(entry.base_url, entry.api)
    rows = []
    for row in models:
        enriched = enrich_model_row(entry.id, row)
        model_row: dict[str, Any] = {
            "id": enriched["id"],
            "name": enriched["id"],
            "api": entry.api,
            "reasoning": bool(enriched.get("reasoning", False)),
            "input": enriched.get("input")
            or (["text", "image"] if enriched.get("supportsVision") else ["text"]),
            "cost": enriched.get("cost")
            or {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "baseUrl": base_url,
            "metadataSource": "models-add",
        }
        if enriched.get("contextWindow") is not None:
            model_row["contextWindow"] = enriched["contextWindow"]
        if enriched.get("maxTokens") is not None:
            model_row["maxTokens"] = enriched["maxTokens"]
        rows.append(model_row)
    out: dict[str, Any] = {"baseUrl": base_url, "api": entry.api}
    if entry.api_key_env:
        out["apiKey"] = f"${{{entry.api_key_env}}}"
    if entry.headers:
        out["headers"] = entry.headers
    out["models"] = rows
    return out


def add_selected_model_to_provider(
    providers: list[ProviderEntry], model_ref: Optional[str]
) -> None:
    """Ensure the selected model's provider lists that model (forward parity)."""
    if not model_ref:
        return
    slash = model_ref.find("/")
    if slash <= 0 or slash == len(model_ref) - 1:
        return
    for provider in providers:
        if provider.id == model_ref[:slash]:
            model_id = model_ref[slash + 1 :]
            if not any(m["id"] == model_id for m in provider.models):
                provider.models.append({"id": model_id})
            return
