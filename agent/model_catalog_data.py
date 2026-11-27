"""Pre-configured model/provider data ported from the external agent (wave 4, item 4).

DATA, not code: the shapes, the bundled rows, and the pure merge/resolution
functions that operate on them. Nothing here touches the network or the
user's config; the live paths that read this module are:

- ``agent/models_dev.py`` — bundled seed rows fill a catalog miss
  (below the models.dev catalog and below Hermes's own builtin rows), and
  ``model_overrides`` ``cost`` / ``compat`` keys are validated here.
- ``agent/usage_pricing.py`` — tiered pricing selection for cost estimates.
- ``hermes_cli/config_providers.py`` — per-model ``compat`` on custom
  provider rows, validated here.

Sources (external-agent HEAD 9b70c509, 2026-11-11):

- Compat shape: ``src/config/zod-schema.model-compat.ts`` (``ModelCompatSchema``,
  ~40 fields). Every field below carries the meaning its external-agent doc comment
  states; the ``hermes`` note says where Hermes already consumes the same
  idea, or that no Hermes reader exists yet. Fields with no Hermes reader
  are still ported as validated data — they are not exposed as config knobs
  anywhere, so nothing can mistake them for a live switch.
- Cost shape + merge/tier math: ``src/config/model-cost.ts``
  (``mergeModelCost``), ``packages/llm-core/src/usage-cost.ts``
  (``normalizeTieredPricing`` / ``selectPricingRates`` /
  ``calculateUsageCost``), ``src/config/zod-schema.core.ts``
  (``ModelDefinitionSchema.cost``).
- Overlay ids: ``src/config/model-provider-overlay-ids.ts`` (verbatim).
- Row merge: ``src/config/model-provider-rows.ts``
  (``mergeNormalizedProviderModel``) and
  ``src/config/model-provider-config.ts``
  (``resolveMergedModelProviderModels``).
- Bundled rows: ``agent/model_catalog_seed.json`` — the authored seed
  rows from the external agent's extension manifests, extracted verbatim (see the
  file's ``_provenance`` block). The external agent fills these in at runtime from
  live discovery; Hermes keeps preferring its models.dev catalog and
  consults these rows only for a per-model gap under a provider the
  catalog already lists — never to resurrect an unlisted provider.

Deliberately NOT ported (with reasons, so a later wave does not re-litigate):

- The TS registry / provider-catalog plugins: Hermes already has
  ``providers/`` + ``plugins/model-providers/`` + the models.dev catalog;
  this item is data + resolution, not a second registry.
- ``openRouterRouting`` / ``vercelGatewayRouting`` as live behaviour:
  Hermes sends no per-route OpenRouter/Vercel routing params, so the
  shapes are validated data only until a request builder consumes them.
- the external agent's remote-catalog bundle + refresh machinery: Hermes's
  equivalent is the models.dev cache in ``agent/models_dev.py``.
- Per-agent ``agents.<id>.utilityModel``: Hermes runs one agent per
  profile, so the equivalent is the top-level ``utilityModel`` key read by
  ``agent/auxiliary_client.py`` (same shape, ``"provider/model"``).
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider overlays
# ---------------------------------------------------------------------------

# Verbatim from the external agent's src/config/model-provider-overlay-ids.ts
# (BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS). The set is the registry of provider
# ids the bundled config knows; the external agent uses it to materialize overlay
# entries for known providers that declare no baseUrl/models of their own
# (src/config/validation-core.ts: materializeBundledModelProviderOverlays).
# Hermes providers are a subset of this set (see providers/ +
# plugins/model-providers/); ids here without a Hermes provider are known
# overlay ids, not Hermes providers — nothing here registers one.
BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS: frozenset[str] = frozenset({
    "amazon-bedrock", "amazon-bedrock-mantle", "anthropic", "anthropic-vertex",
    "arcee", "azure-openai-responses", "byteplus", "byteplus-plan", "cerebras",
    "chutes", "claude-cli", "clawrouter", "cloudflare-ai-gateway", "codex",
    "comfy", "copilot-proxy", "dashscope", "deepinfra", "deepseek", "fal",
    "fireworks", "github-copilot", "gmi", "gmi-cloud", "gmicloud", "google",
    "google-antigravity", "google-gemini-cli", "google-vertex", "groq",
    "huggingface", "kie", "kilocode", "kimi", "kimi-coding", "litellm",
    "lmstudio", "meta", "microsoft-foundry", "minimax", "minimax-portal",
    "mistral", "modelstudio", "moonshot", "moonshot-ai", "moonshotai",
    "nvidia", "novita", "novita-ai", "novitaai", "ollama", "ollama-cloud",
    "openai", "opencode", "opencode-go", "openrouter", "qianfan", "qwen",
    "qwen-token-plan", "qwencloud", "requesty", "runway", "sglang",
    "stepfun", "stepfun-plan", "synthetic", "tencent-tokenhub",
    "tencent-tokenplan", "together", "venice", "vercel-ai-gateway", "vllm",
    "volcengine", "volcengine-plan", "vydra", "x-ai", "xai", "xiaomi",
    "xiaomi-token-plan", "z.ai", "z-ai", "zai",
})


def normalize_provider_id(provider_id: Any) -> str:
    """The external agent's provider-id normalization: trim + lowercase.

    Mirrors ``normalizeProviderId`` (packages/model-catalog-core/src/
    provider-id.ts), which is ``normalizeLowercaseStringOrEmpty`` — no
    separator or alias folding, so ``z.ai`` and ``z-ai`` stay distinct ids.
    """
    return str(provider_id or "").strip().lower()


def is_built_in_model_provider_overlay_id(provider_id: Any) -> bool:
    """True when *provider_id* is a known bundled overlay id (after normalization)."""
    return normalize_provider_id(provider_id) in BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS


# ---------------------------------------------------------------------------
# Compat flags
# ---------------------------------------------------------------------------

# Thinking levels / formats, from the external agent's packages/llm-core/src/model-data.ts
# (MODEL_DATA_THINKING_LEVELS) and src/config/model-config-vocabulary.ts
# (MODEL_THINKING_FORMATS).
THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
THINKING_FORMATS: tuple[str, ...] = (
    "openai", "openrouter", "deepseek", "together", "qwen", "qwen-chat-template", "zai",
)
REASONING_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# One entry per ModelCompatSchema field. ``kind`` is the validation shape;
# ``meaning`` is what the external agent's schema comment says the flag does;
# ``hermes`` is the traced Hermes equivalent (or the honest "no reader yet").
# Routing sub-objects (openRouterRouting / vercelGatewayRouting) are nested
# dicts whose fields are listed under ``fields``.
MODEL_COMPAT_FIELDS: dict[str, dict[str, Any]] = {
    "supportsStore": {
        "kind": "bool", "default": None,
        "meaning": "Provider supports the `store` field (auto-detected from URL when unset).",
        "hermes": "No Hermes reader; request builders never send `store` per model.",
    },
    "supportsPromptCacheKey": {
        "kind": "bool", "default": None,
        "meaning": "Provider accepts prompt-cache / session-affinity keys.",
        "hermes": "Live: ProviderProfile.supports_prompt_cache_key (provider level) and the "
        "per-model `prompt_caching` capability read by agent_runtime_helpers.",
    },
    "supportsResponsesContinuation": {
        "kind": "bool", "default": None,
        "meaning": "Model opts into stored HTTP continuation on a verified compatible endpoint.",
        "hermes": "No Hermes reader; no stored-continuation path exists.",
    },
    "supportsDeveloperRole": {
        "kind": "bool", "default": None,
        "meaning": "Provider supports the `developer` role instead of `system` (auto-detected from URL).",
        "hermes": "No Hermes reader; role mapping is transport-decided, not per-model data.",
    },
    "supportsReasoningEffort": {
        "kind": "bool", "default": None,
        "meaning": "Provider supports `reasoning_effort` (auto-detected from URL).",
        "hermes": "Live, provider-level: ProviderProfile.default_reasoning_config / "
        "setup_reasoning_efforts, gated by model_overrides supports_reasoning.",
    },
    "supportsTemperature": {
        "kind": "bool", "default": True,
        "meaning": "Model accepts the `temperature` parameter (default true).",
        "hermes": "Data only: carried as ModelInfo.temperature from the catalog; "
        "no live reader gates temperature on it yet.",
    },
    "supportsInstructions": {
        "kind": "bool", "default": None,
        "meaning": "Provider honors top-level `instructions`; true only for verified native "
        "routes (OpenAI, xAI), otherwise the system prompt is embedded in `input`.",
        "hermes": "No Hermes reader; Hermes always sends the system prompt as a message.",
    },
    "supportsUsageInStreaming": {
        "kind": "bool", "default": True,
        "meaning": "Provider supports `stream_options: {include_usage: true}` for token usage in streams.",
        "hermes": "Data only: 86 bundled rows author it; no live reader branches on it yet.",
    },
    "supportsTools": {
        "kind": "bool", "default": None,
        "meaning": "Model supports tool/function calling.",
        "hermes": "Live: supports_tools / ModelInfo.tool_call via get_model_capabilities.",
    },
    "codeMode": {
        "kind": "enum", "values": ("preferred", "capable"), "default": None,
        "meaning": "Code-mode tier consumed by `tools.codeMode.enabled: 'auto'`; absent means 'capable'.",
        "hermes": "Data only: 96 bundled rows author it; Hermes has no code-mode tier.",
    },
    "supportsStrictMode": {
        "kind": "bool", "default": True,
        "meaning": "Provider supports the `strict` field in tool definitions (default true).",
        "hermes": "No Hermes reader; tool schemas are built without a per-model strict flag.",
    },
    "supportsJsonSchemaResponseFormat": {
        "kind": "bool", "default": False,
        "meaning": "Provider supports JSON Schema through `response_format` (default false for unknown endpoints).",
        "hermes": "Data only: carried as ModelInfo.structured_output from the catalog; "
        "aux structured-output degrades on rejection rather than gating on it.",
    },
    "requiresStringContent": {
        "kind": "bool", "default": None,
        "meaning": "All message parts must be coerced to plain strings.",
        "hermes": "No Hermes reader; message conversion is transport-owned.",
    },
    "strictMessageKeys": {
        "kind": "bool", "default": None,
        "meaning": "Unknown message payload keys must be stripped before requests.",
        "hermes": "No Hermes reader; message conversion is transport-owned.",
    },
    "visibleReasoningDetailTypes": {
        "kind": "str-list", "default": None,
        "meaning": "Reasoning detail block types safe to expose in visible transcripts.",
        "hermes": "No Hermes reader; reasoning_details pass through opaquely.",
    },
    "supportedReasoningEfforts": {
        "kind": "str-list", "default": None,
        "meaning": "Provider-accepted reasoning effort labels.",
        "hermes": "Partial: reasoning effort labels are provider-profile owned "
        "(setup_reasoning_efforts); no per-model list is consumed yet.",
    },
    "reasoningEffortMap": {
        "kind": "str-map", "default": None,
        "meaning": "Per-level reasoning effort overrides, e.g. map 'off' to 'low' for models that cannot disable thinking.",
        "hermes": "No Hermes reader; no per-model effort remapping exists.",
    },
    "defaultThinkingLevel": {
        "kind": "str", "default": None,
        "meaning": "Thinking level a new session starts at; when set, this row owns the thinking profile.",
        "hermes": "No Hermes reader; session thinking level is not per-model data.",
    },
    "thinkingLevels": {
        "kind": "str-list", "default": None,
        "meaning": "Offered thinking levels, in order; when set, this row owns the thinking profile.",
        "hermes": "No Hermes reader; offered levels come from the provider profile.",
    },
    "adaptiveThinking": {
        "kind": "bool", "default": None,
        "meaning": "Offers the `adaptive` thinking level.",
        "hermes": "No Hermes reader.",
    },
    "thinkingRequired": {
        "kind": "bool", "default": None,
        "meaning": "Thinking stays on even when `reasoning` is false.",
        "hermes": "No Hermes reader.",
    },
    "thinkingLevelLabels": {
        "kind": "str-map", "default": None,
        "meaning": "Display labels per thinking level, e.g. `{low: 'on'}`.",
        "hermes": "No Hermes reader.",
    },
    "maxTokensField": {
        "kind": "enum", "values": ("max_completion_tokens", "max_tokens"), "default": None,
        "meaning": "Which field to use for max tokens (auto-detected from URL when unset).",
        "hermes": "No Hermes reader; max-tokens field selection is transport-decided.",
    },
    "thinkingFormat": {
        "kind": "enum", "values": THINKING_FORMATS, "default": None,
        "meaning": "Reasoning/thinking payload dialect for provider-compatible APIs.",
        "hermes": "No Hermes reader; thinking payload dialect is transport-owned.",
    },
    "requiresToolResultName": {
        "kind": "bool", "default": None,
        "meaning": "Tool results require the `name` field (auto-detected from URL).",
        "hermes": "No Hermes reader; tool-result shaping is transport-owned.",
    },
    "requiresAssistantAfterToolResult": {
        "kind": "bool", "default": None,
        "meaning": "A user message after tool results requires an assistant message in between.",
        "hermes": "Subsumed: Hermes enforces strict role alternation globally "
        "(agent/AGENTS.md), so no per-model flag is needed.",
    },
    "requiresThinkingAsText": {
        "kind": "bool", "default": None,
        "meaning": "Thinking blocks must be converted to text blocks with <thinking> delimiters.",
        "hermes": "No Hermes reader; thinking-block conversion is transport-owned.",
    },
    "requiresReasoningContentOnAssistantMessages": {
        "kind": "bool", "default": None,
        "meaning": "All replayed assistant messages must include an empty reasoning_content field when reasoning is on.",
        "hermes": "No Hermes reader; replay shaping is transport-owned.",
    },
    "toolSchemaProfile": {
        "kind": "str", "default": None,
        "meaning": "Named tool-schema profile used by provider adapters.",
        "hermes": "No Hermes reader; no named tool-schema profiles exist.",
    },
    "unsupportedToolSchemaKeywords": {
        "kind": "str-list", "default": None,
        "meaning": "JSON Schema keywords rejected by this provider's tool schema validator.",
        "hermes": "No Hermes reader; tool schemas are not per-model filtered.",
    },
    "toolCallArgumentsEncoding": {
        "kind": "str", "default": None,
        "meaning": "Encoding expected for tool-call arguments in provider payloads.",
        "hermes": "No Hermes reader.",
    },
    "requiresOpenAiAnthropicToolPayload": {
        "kind": "bool", "default": None,
        "meaning": "OpenAI-style calls must be reshaped to Anthropic-compatible tool payloads.",
        "hermes": "No Hermes reader; the Anthropic adapter owns its own payload shape.",
    },
    "openRouterRouting": {
        "kind": "routing", "default": None,
        "meaning": "OpenRouter-specific routing preferences; only used when baseUrl points to OpenRouter.",
        "hermes": "Not portable as behaviour: Hermes sends no per-route OpenRouter "
        "routing params. Validated data only (fields: allow_fallbacks, "
        "require_parameters, data_collection, zdr, enforce_distillable_text, "
        "order, only, ignore, quantizations, sort, max_price, "
        "preferred_min_throughput, preferred_max_latency).",
        "fields": {
            "allow_fallbacks": "bool", "require_parameters": "bool",
            "data_collection": ("deny", "allow"), "zdr": "bool",
            "enforce_distillable_text": "bool", "order": "str-list",
            "only": "str-list", "ignore": "str-list", "quantizations": "str-list",
            "sort": "any", "max_price": "max-price",
            "preferred_min_throughput": "number-or-percentiles",
            "preferred_max_latency": "number-or-percentiles",
        },
    },
    "vercelGatewayRouting": {
        "kind": "routing", "default": None,
        "meaning": "Vercel AI Gateway routing preferences; only used when baseUrl points to the gateway.",
        "hermes": "Not portable as behaviour: Hermes sends no per-route gateway "
        "routing params. Validated data only (fields: only, order).",
        "fields": {"only": "str-list", "order": "str-list"},
    },
    "zaiToolStream": {
        "kind": "bool", "default": False,
        "meaning": "Whether z.ai supports top-level `tool_stream: true` for streaming tool call deltas (default false).",
        "hermes": "No Hermes reader; z.ai streaming is transport-owned.",
    },
    "cacheControlFormat": {
        "kind": "enum", "values": ("anthropic",), "default": None,
        "meaning": "Cache control convention for prompt caching; 'anthropic' applies Anthropic-style cache_control markers.",
        "hermes": "Transport-decided: Hermes applies cache markers by wire format "
        "(agent_runtime_helpers), not from per-model data.",
    },
    "sendSessionAffinityHeaders": {
        "kind": "bool", "default": False,
        "meaning": "Send known session-affinity headers (session_id, x-client-request-id, x-session-affinity) when caching is on.",
        "hermes": "Live, provider-level: session_affinity_header on the provider entry "
        "(opt-in per provider; Hermes never ships a session id unasked).",
    },
    "sendSessionIdHeader": {
        "kind": "bool", "default": True,
        "meaning": "Send the OpenAI `session_id` cache-affinity header when caching is on (default true).",
        "hermes": "No Hermes reader; see sendSessionAffinityHeaders for the provider-level knob.",
    },
    "supportsEagerToolInputStreaming": {
        "kind": "bool", "default": True,
        "meaning": "Provider accepts per-tool `eager_input_streaming`; when false the Anthropic provider omits it and sends the legacy fine-grained-tool-streaming beta header.",
        "hermes": "No Hermes reader; Anthropic streaming headers are transport-owned.",
    },
    "supportsLongCacheRetention": {
        "kind": "bool", "default": True,
        "meaning": "Provider supports long prompt cache retention (24h / 1h TTL, depending on format).",
        "hermes": "No Hermes reader.",
    },
}

# Routing sub-object field kinds, for the two `routing` compat fields above.
_ROUTING_FIELD_KINDS: dict[str, str] = {
    "bool": "bool", "str-list": "str-list", "any": "any",
    "max-price": "max-price", "number-or-percentiles": "number-or-percentiles",
}


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _clean_str_list(value: Any) -> Optional[list[str]]:
    if not isinstance(value, list):
        return None
    cleaned = [item for item in value if _is_nonempty_str(item)]
    return cleaned if cleaned else None


def _clean_str_map(value: Any) -> Optional[dict[str, str]]:
    if not isinstance(value, dict):
        return None
    cleaned = {str(k): str(v) for k, v in value.items() if _is_nonempty_str(k) and _is_nonempty_str(v)}
    return cleaned or None


def _clean_routing(value: Any, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Validate one routing sub-object (openRouterRouting / vercelGatewayRouting).

    Unknown keys are dropped; each known key is checked against its declared
    kind. ``max_price`` keeps only its known per-unit keys (prompt /
    completion / image / audio / request, number or numeric string).
    Percentile cutoffs keep only p50/p75/p90/p99 numbers.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, raw in value.items():
        kind = fields.get(key)
        if kind is None:
            continue
        if kind == "bool":
            if isinstance(raw, bool):
                out[key] = raw
        elif kind == "str-list":
            cleaned = _clean_str_list(raw)
            if cleaned is not None:
                out[key] = cleaned
        elif kind == "any":
            out[key] = raw
        elif isinstance(kind, tuple):  # enum
            if raw in kind:
                out[key] = raw
        elif kind == "max-price":
            if isinstance(raw, dict):
                prices = {
                    unit: price for unit, price in raw.items()
                    if unit in {"prompt", "completion", "image", "audio", "request"}
                    and (isinstance(price, (int, float)) and math.isfinite(price)
                         or isinstance(price, str) and price.strip())
                }
                if prices:
                    out[key] = prices
        elif kind == "number-or-percentiles":
            if isinstance(raw, (int, float)) and math.isfinite(raw):
                out[key] = raw
            elif isinstance(raw, dict):
                cuts = {p: v for p, v in raw.items()
                        if p in {"p50", "p75", "p90", "p99"}
                        and isinstance(v, (int, float)) and math.isfinite(v)}
                if cuts:
                    out[key] = cuts
    return out or None


def _compat_bool(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, Optional[str]]:
    if isinstance(value, bool):
        return value, None
    return None, f"compat.{key}: expected a boolean, ignored"


def _compat_enum(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, Optional[str]]:
    if value in spec["values"]:
        return value, None
    return None, f"compat.{key}: expected one of {spec['values']}, ignored"


def _compat_str(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, Optional[str]]:
    if _is_nonempty_str(value):
        return value, None
    return None, f"compat.{key}: expected a non-empty string, ignored"


def _compat_str_list(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, Optional[str]]:
    items = _clean_str_list(value)
    if items is not None:
        return items, None
    return None, f"compat.{key}: expected a list of non-empty strings, ignored"


def _compat_str_map(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, Optional[str]]:
    mapping = _clean_str_map(value)
    if mapping is not None:
        return mapping, None
    return None, f"compat.{key}: expected a string-to-string mapping, ignored"


def _compat_routing(key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, Optional[str]]:
    routed = _clean_routing(value, spec["fields"])
    if routed is not None:
        return routed, None
    return None, f"compat.{key}: expected a mapping, ignored"


# Per-kind compat validators (HX011: table, not an if/elif ladder). A validator
# returns ``(cleaned_value, None)`` on success or ``(None, warning)`` when the
# value is dropped. Partially-cleaned lists/maps/routing pass with only the
# bad element dropped silently, as before.
_COMPAT_KIND_VALIDATORS: dict[str, Any] = {
    "bool": _compat_bool,
    "enum": _compat_enum,
    "str": _compat_str,
    "str-list": _compat_str_list,
    "str-map": _compat_str_map,
    "routing": _compat_routing,
}


def validate_model_compat(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a ``compat`` mapping against the ported ModelCompatSchema.

    Returns ``(cleaned, warnings)``. Unknown keys and wrong-typed values are
    dropped (with a warning each); valid values pass through unchanged.
    Fail-open like the rest of Hermes's config reads: bad data never breaks
    resolution, it just does not apply.
    """
    if not isinstance(raw, dict):
        return {}, ["compat must be a mapping"] if raw is not None else []
    cleaned: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in raw.items():
        spec = MODEL_COMPAT_FIELDS.get(key)
        if spec is None:
            warnings.append(f"compat: unknown key {key!r} ignored")
            continue
        validator = _COMPAT_KIND_VALIDATORS.get(spec["kind"])
        if validator is None:
            continue
        cleaned_value, warning = validator(key, value, spec)
        if warning is not None:
            warnings.append(warning)
        else:
            cleaned[key] = cleaned_value
    return cleaned, warnings


# ---------------------------------------------------------------------------
# Cost: flat rates + tiered pricing (USD per million tokens)
# ---------------------------------------------------------------------------

COST_RATE_KEYS: tuple[str, ...] = ("input", "output", "cacheRead", "cacheWrite")


def _finite_nonnegative(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def normalize_tiered_pricing(raw: Any) -> Optional[list[dict[str, Any]]]:
    """Normalize a ``tieredPricing`` list (port of normalizeTieredPricing).

    Each tier needs all four rates (finite, non-negative) and a ``range`` of
    ``[start]`` (open-ended top tier) or ``[start, end)`` with end > start.
    Invalid tiers are dropped; survivors sort by range start. An empty result
    is None (no tiers), never an empty list.
    """
    if not isinstance(raw, list):
        return None
    tiers: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rates = {key: _finite_nonnegative(item.get(key)) for key in COST_RATE_KEYS}
        if any(rate is None for rate in rates.values()):
            continue
        raw_range = item.get("range")
        if not isinstance(raw_range, list) or not raw_range:
            continue
        start = _finite_nonnegative(raw_range[0])
        if start is None:
            continue
        end: Optional[float] = None
        if len(raw_range) >= 2:
            end = _finite_nonnegative(raw_range[1])
            if end is None or end <= start:
                continue
        tiers.append({**rates, "range": [start] if end is None else [start, end]})
    if not tiers:
        return None
    tiers.sort(key=lambda tier: tier["range"][0])
    return tiers


def normalize_model_cost(raw: Any) -> Optional[dict[str, Any]]:
    """Normalize a ``cost`` mapping (port of normalizeModelCostConfig).

    Flat rates are optional here (a tiers-only cost is valid); every present
    rate must be finite and non-negative. ``tieredPricing`` normalizes via
    ``normalize_tiered_pricing``. Returns None when nothing usable survives.
    Rates accept the external-agent camelCase spellings (``cacheRead`` /
    ``cacheWrite``) and the models.dev snake_case aliases (``cache_read`` /
    ``cache_write``) — Hermes config is snake_case by convention, the external agent
    data is camelCase; camelCase wins when both spellings are present.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in COST_RATE_KEYS:
        rate = _finite_nonnegative(raw.get(key)) if key in raw else None
        if rate is None:
            alias = {"cacheRead": "cache_read", "cacheWrite": "cache_write"}.get(key)
            rate = _finite_nonnegative(raw.get(alias)) if alias and alias in raw else None
        if rate is not None:
            out[key] = rate
    tiers = normalize_tiered_pricing(raw.get("tieredPricing"))
    if tiers is not None:
        out["tieredPricing"] = tiers
    return out or None


def merge_model_cost(
    lower_priority: Optional[dict[str, Any]],
    higher_priority: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Merge two cost mappings (port of mergeModelCost).

    The higher-priority mapping wins outright; an empty higher mapping keeps
    the lower one. Crucially, inherited tiers never silently replace authored
    flat or zero pricing: when the higher mapping has any keys, the lower
    mapping's ``tieredPricing`` is dropped (its flat rates are overridden
    key-by-key, and the tiers belong to the lower mapping's rates).
    """
    if not lower_priority or not higher_priority:
        return higher_priority or lower_priority
    if not higher_priority:
        return lower_priority
    lower = {key: value for key, value in lower_priority.items() if key != "tieredPricing"}
    return {**lower, **higher_priority}


# models.dev catalog entries carry snake_case flat rates (``cache_read`` /
# ``cache_write``); external-agent cost mappings carry camelCase (``cacheRead`` /
# ``cacheWrite``). Tier dicts stay camelCase in both shapes — they are nested
# Seed data, converted to ``CostTier`` by agent/usage_pricing.py.
_CATALOG_FLAT_KEYS: dict[str, str] = {
    "input": "input", "output": "output",
    "cacheRead": "cache_read", "cacheWrite": "cache_write",
}
_SEED_FLAT_KEYS: dict[str, str] = {v: k for k, v in _CATALOG_FLAT_KEYS.items()}


def seed_cost_to_catalog_shape(cost: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Translate an external-agent-shaped cost mapping to the models.dev catalog shape."""
    if not isinstance(cost, dict):
        return None
    out: dict[str, Any] = {}
    for seed_key, catalog_key in _CATALOG_FLAT_KEYS.items():
        if seed_key in cost:
            out[catalog_key] = cost[seed_key]
    if cost.get("tieredPricing") is not None:
        out["tieredPricing"] = cost["tieredPricing"]
    return out or None


def catalog_cost_to_seed_shape(cost: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Translate a models.dev-shaped cost mapping to the external-agent shape."""
    if not isinstance(cost, dict):
        return None
    out: dict[str, Any] = {}
    for catalog_key, seed_key in _SEED_FLAT_KEYS.items():
        if catalog_key in cost:
            out[seed_key] = cost[catalog_key]
    if cost.get("tieredPricing") is not None:
        out["tieredPricing"] = cost["tieredPricing"]
    return out or None


def select_tier_index(ranges: list[tuple[float, Optional[float]]], prompt_tokens: float) -> int:
    """Pick the tier index for *prompt_tokens* (port of selectPricingRates).

    ``ranges`` are ``(start, end-or-None)`` half-open intervals, sorted by
    start. Exact match wins; gaps and overflow fall back to the nearest
    lower tier, else the first tier; a non-positive token count takes the
    first tier. One whole-request tier is selected — buckets are never
    blended across tiers.
    """
    if not ranges:
        return -1
    if prompt_tokens <= 0:
        return 0
    for index, (start, end) in enumerate(ranges):
        if prompt_tokens >= start and (end is None or prompt_tokens < end):
            return index
    lower = [index for index, (start, _end) in enumerate(ranges) if prompt_tokens >= start]
    if lower:
        return lower[-1]
    return 0


def select_pricing_tier(cost: dict[str, Any], prompt_tokens: float) -> dict[str, Any]:
    """Select the flat rates for one request (port of selectPricingRates).

    Without tiers the cost itself is the answer. With tiers, the tier whose
    half-open range holds ``prompt_tokens`` (input + cache-read + cache-write
    tokens — the same axis as Hermes's ``CanonicalUsage.prompt_tokens``)
    supplies all four rates for the whole request.
    """
    tiers = cost.get("tieredPricing")
    if not isinstance(tiers, list) or not tiers:
        return {key: cost.get(key, 0) for key in COST_RATE_KEYS}
    ranges = [
        (float(tier["range"][0]),
         float(tier["range"][1]) if len(tier["range"]) > 1 else None)
        for tier in tiers
    ]
    index = select_tier_index(ranges, prompt_tokens)
    tier = tiers[index]
    return {key: tier.get(key, 0) for key in COST_RATE_KEYS}


def calculate_tiered_cost(usage: dict[str, float], cost: dict[str, Any]) -> dict[str, float]:
    """Price one model call (port of calculateUsageCost, float math).

    ``usage`` carries input / output / cacheRead / cacheWrite token counts.
    The tier is selected on the prompt-token axis (input + cacheRead +
    cacheWrite) and its rates bill every bucket for the whole request.
    The external agent additionally prices a one-hour cache-write subset at twice the
    input rate; Hermes usage records carry no cache-write retention split,
    so that sub-rule has no input here and is not applied.
    """
    rates = select_pricing_tier(cost, usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0))
    priced = {
        "input": usage.get("input", 0) * rates["input"] / 1_000_000,
        "output": usage.get("output", 0) * rates["output"] / 1_000_000,
        "cacheRead": usage.get("cacheRead", 0) * rates["cacheRead"] / 1_000_000,
        "cacheWrite": usage.get("cacheWrite", 0) * rates["cacheWrite"] / 1_000_000,
    }
    priced["total"] = sum(priced.values())
    return priced


# ---------------------------------------------------------------------------
# Provider model rows: merge + resolve (ports of model-provider-rows.ts /
# model-provider-config.ts)
# ---------------------------------------------------------------------------

def merge_provider_model_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge authored model rows by id (port of resolveMergedModelProviderModels).

    Earlier rows stay authoritative, including explicit empty objects; later
    duplicates only supply top-level fields the first row omitted. ``cost``
    merges via ``merge_model_cost`` (the earlier row is higher priority), so
    a later row's tiers never silently replace an earlier row's flat rates.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        existing = merged.get(model_id)
        if existing is None:
            merged[model_id] = dict(row)
            continue
        combined = {**row, **existing}
        if "cost" in row or "cost" in existing:
            cost = merge_model_cost(row.get("cost"), existing.get("cost"))
            if cost is not None:
                combined["cost"] = cost
            else:
                combined.pop("cost", None)
        merged[model_id] = combined
    return merged


def resolve_configured_provider_model(
    models: Any, provider: str, model_id: str,
) -> Optional[dict[str, Any]]:
    """Find one configured model row (port of createConfiguredProviderModelResolver, core leg).

    Exact id first, then case-insensitive, then the legacy self-provider
    prefix (``<provider>/`` or ``<provider>:`` stripped once — the external agent
    strips ``stripSelfProviderModelPrefix`` the same way for legacy rows).
    Model ids here are exact-match first because Hermes's own ids (e.g.
    OpenRouter ``google/gemini-...`` slugs) already carry their vendor.
    """
    if not isinstance(models, (list, dict)) or not model_id:
        return None
    rows = merge_provider_model_rows(
        list(models.values()) if isinstance(models, dict)
        else [row for row in models if isinstance(row, dict)])
    wanted = str(model_id).strip()
    if wanted in rows:
        return rows[wanted]
    lowered = wanted.lower()
    for key, row in rows.items():
        if key.lower() == lowered:
            return row
    prefix = normalize_provider_id(provider)
    for sep in ("/", ":"):
        token = f"{prefix}{sep}"
        if lowered.startswith(token):
            stripped = wanted[len(token):]
            if stripped in rows:
                return rows[stripped]
            for key, row in rows.items():
                if key.lower() == stripped.lower():
                    return row
    return None


# ---------------------------------------------------------------------------
# Bundled seed rows (agent/model_catalog_seed.json)
# ---------------------------------------------------------------------------

_BUNDLED_CATALOG: Optional[dict[str, Any]] = None
_BUNDLED_CATALOG_LOADED = False


def _bundled_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "model_catalog_seed.json"


def load_bundled_seed_catalog() -> dict[str, Any]:
    """The bundled seed snapshot (``{provider: {baseUrl?, api?, models}}``).

    Loaded once per process from the JSON next to this module; a missing or
    corrupt file logs once and reads as empty — the catalog is a fallback
    layer, never load-bearing.
    """
    global _BUNDLED_CATALOG, _BUNDLED_CATALOG_LOADED
    if _BUNDLED_CATALOG_LOADED:
        return _BUNDLED_CATALOG or {}
    _BUNDLED_CATALOG_LOADED = True
    try:
        with open(_bundled_catalog_path(), encoding="utf-8-sig") as fh:
            snapshot = json.load(fh)
        providers = snapshot.get("providers")
        _BUNDLED_CATALOG = providers if isinstance(providers, dict) else {}
    except Exception:
        logger.warning("bundled seed model catalog unreadable; continuing without it", exc_info=True)
        _BUNDLED_CATALOG = {}
    return _BUNDLED_CATALOG


def seed_row_to_catalog_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Translate one seed model row into a models.dev-shaped entry.

    Only fields Hermes's catalog shape understands are translated; the rest
    (``thinkingLevelMap``, ``mediaInput``, ``contextWindows``,
    ``contextWindowDefault``, ``status`` / ``replacedBy``) ride along
    untouched so the data is not lost when a reader grows one. ``compat``
    is validated; ``cost`` is normalized (flat + tieredPricing). Fields the
    row does not claim stay absent — an absent claim is unknown, never a
    synthesized value.
    """
    entry: dict[str, Any] = {}
    if _is_nonempty_str(row.get("name")):
        entry["name"] = row["name"]
    if isinstance(row.get("reasoning"), bool):
        entry["reasoning"] = row["reasoning"]
    if _is_nonempty_str(row.get("family")):
        entry["family"] = row["family"]
    if _is_nonempty_str(row.get("status")):
        entry["status"] = row["status"]
    if _is_nonempty_str(row.get("replacedBy")):
        entry["replacedBy"] = row["replacedBy"]
    limit: dict[str, Any] = {}
    context_window = _finite_nonnegative(row.get("contextWindow"))
    if context_window is not None and context_window > 0:
        limit["context"] = int(context_window)
    max_tokens = _finite_nonnegative(row.get("maxTokens"))
    if max_tokens is not None and max_tokens > 0:
        limit["output"] = int(max_tokens)
    if limit:
        entry["limit"] = limit
    input_mods = _clean_str_list(row.get("input"))
    if input_mods is not None:
        entry["modalities"] = {"input": input_mods, "output": ["text"]}
    compat, _warnings = validate_model_compat(row.get("compat"))
    if compat:
        entry["compat"] = compat
        if isinstance(compat.get("supportsTools"), bool):
            entry["tool_call"] = compat["supportsTools"]
    cost = normalize_model_cost(row.get("cost"))
    if cost is not None:
        entry["cost"] = cost
    for key in ("thinkingLevelMap", "mediaInput", "contextWindows", "contextWindowDefault"):
        if row.get(key) is not None:
            entry[key] = row[key]
    return entry


def bundled_seed_model(provider: str, model: str) -> Optional[dict[str, Any]]:
    """Bundled seed row for (provider, model) as a catalog-shaped entry, or None.

    Exact id first, then case-insensitive — the same match order
    ``models_dev`` uses for its catalog. Provider ids match after the same
    normalization the overlay predicate uses.
    """
    if not provider or not model:
        return None
    catalog = load_bundled_seed_catalog()
    wanted_provider = normalize_provider_id(provider)
    section: Optional[dict[str, Any]] = None
    for key, value in catalog.items():
        if normalize_provider_id(key) == wanted_provider and isinstance(value, dict):
            section = value
            break
    if section is None:
        return None
    rows = section.get("models")
    if not isinstance(rows, list):
        return None
    row = resolve_configured_provider_model(rows, provider, model)
    if row is None:
        return None
    return seed_row_to_catalog_entry(row)
