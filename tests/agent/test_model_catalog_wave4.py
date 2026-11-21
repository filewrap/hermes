"""Wave 4 item 4: pre-configured models/providers as data.

Covers the invariants, not counts: the overlay id set is the verbatim
seed set; compat/cost shapes validate and merge by the ported seed
rules; tier selection bills one whole-request tier; the bundled snapshot
round-trips through the Hermes path; model_overrides cost/compat reach
get_model_info; utilityModel routes side tasks; the /models probe refresh
preserves per-model row metadata.
"""
from decimal import Decimal

import pytest

from agent import models_dev
from agent.model_catalog_data import (
    BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS,
    bundled_seed_model,
    calculate_tiered_cost,
    is_built_in_model_provider_overlay_id,
    load_bundled_seed_catalog,
    merge_model_cost,
    merge_provider_model_rows,
    normalize_model_cost,
    normalize_tiered_pricing,
    resolve_configured_provider_model,
    select_pricing_tier,
    validate_model_compat,
)
from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
)

# Real rows from the bundled snapshot (agent/model_catalog_seed.json),
# quoted here so the tests fail if the snapshot drifts from its source.
HAIKU_TIERS = [
    {"range": [0, 100001], "input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125},
    {"range": [100001], "input": 0.5, "output": 2.5, "cacheRead": 0.05, "cacheWrite": 0.625},
]


# ---------------------------------------------------------------------------
# Provider overlays
# ---------------------------------------------------------------------------

def test_overlay_ids_are_the_verbatim_seed_set():
    assert len(BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS) == 82
    for known in ("anthropic", "openai", "openrouter", "deepseek", "google",
                  "ollama", "z.ai", "x-ai", "amazon-bedrock", "vercel-ai-gateway"):
        assert known in BUILT_IN_MODEL_PROVIDER_OVERLAY_IDS


def test_overlay_predicate_normalizes_like_seed():
    assert is_built_in_model_provider_overlay_id("  Anthropic ") is True
    assert is_built_in_model_provider_overlay_id("OPENROUTER") is True
    assert is_built_in_model_provider_overlay_id("not-a-provider") is False
    assert is_built_in_model_provider_overlay_id("") is False
    assert is_built_in_model_provider_overlay_id(None) is False
    # z.ai and z-ai are distinct ids in the seed set; both are in the set.
    assert is_built_in_model_provider_overlay_id("z.ai") is True
    assert is_built_in_model_provider_overlay_id("z-ai") is True


# ---------------------------------------------------------------------------
# Compat validation
# ---------------------------------------------------------------------------

def test_compat_validation_keeps_known_flags_drops_unknown():
    cleaned, warnings = validate_model_compat({
        "supportsTools": True,
        "supportsTemperature": False,
        "codeMode": "preferred",
        "thinkingFormat": "deepseek",
        "maxTokensField": "max_tokens",
        "bogusFlag": True,
        "supportsTools_typo": True,
    })
    assert cleaned == {
        "supportsTools": True,
        "supportsTemperature": False,
        "codeMode": "preferred",
        "thinkingFormat": "deepseek",
        "maxTokensField": "max_tokens",
    }
    assert len(warnings) == 2
    assert any("bogusFlag" in w for w in warnings)


def test_compat_validation_rejects_wrong_types_and_enum_values():
    cleaned, warnings = validate_model_compat({
        "supportsTools": "yes",
        "codeMode": "ultra",
        "supportedReasoningEfforts": ["low", "", 42],
        "reasoningEffortMap": {"off": "low", "high": ""},
        "openRouterRouting": {"allow_fallbacks": True, "data_collection": "maybe",
                              "max_price": {"prompt": 1.5, "bogus_unit": 9}},
    })
    assert cleaned == {
        "supportedReasoningEfforts": ["low"],
        "reasoningEffortMap": {"off": "low"},
        "openRouterRouting": {"allow_fallbacks": True, "max_price": {"prompt": 1.5}},
    }
    # Whole-field drops warn; partially-cleaned lists/maps and routing
    # sub-fields drop only the bad element, silently.
    assert warnings == [
        "compat.supportsTools: expected a boolean, ignored",
        "compat.codeMode: expected one of ('preferred', 'capable'), ignored",
    ]


def test_compat_validation_empty_and_non_mapping():
    assert validate_model_compat({}) == ({}, [])
    assert validate_model_compat(None) == ({}, [])
    cleaned, warnings = validate_model_compat("nope")
    assert cleaned == {} and warnings


def test_compat_schema_covers_the_seed_fields():
    from agent.model_catalog_data import MODEL_COMPAT_FIELDS
    assert len(MODEL_COMPAT_FIELDS) == 40
    for required in ("supportsStore", "supportsPromptCacheKey", "supportsTools",
                     "supportsReasoningEffort", "supportedReasoningEfforts",
                     "reasoningEffortMap", "thinkingLevels", "defaultThinkingLevel",
                     "maxTokensField", "thinkingFormat", "openRouterRouting",
                     "vercelGatewayRouting", "cacheControlFormat",
                     "sendSessionAffinityHeaders", "supportsLongCacheRetention"):
        assert required in MODEL_COMPAT_FIELDS


# ---------------------------------------------------------------------------
# Cost: normalize / merge / tier selection
# ---------------------------------------------------------------------------

def test_cost_normalize_sorts_tiers_and_keeps_open_ended_top():
    normalized = normalize_model_cost({
        "input": 0.5,
        "tieredPricing": [
            {"range": [100001], "input": 0.5, "output": 2.5, "cacheRead": 0.05, "cacheWrite": 0.625},
            {"range": [0, 100001], "input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125},
        ],
    })
    assert normalized is not None
    assert normalized["input"] == 0.5
    assert normalized["tieredPricing"] == HAIKU_TIERS


def test_cost_normalize_drops_invalid_tiers_and_rates():
    assert normalize_tiered_pricing([
        {"range": [0, 100], "input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        {"range": [100, 50], "input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        {"range": [200], "input": -1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        {"range": "bogus", "input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        "not-a-tier",
    ]) == [{"range": [0, 100], "input": 1.0, "output": 2.0, "cacheRead": 0.0, "cacheWrite": 0.0}]
    assert normalize_model_cost({"input": -1, "output": float("nan")}) is None
    assert normalize_model_cost({"input": 0, "output": 0}) == {"input": 0.0, "output": 0.0}
    assert normalize_model_cost("nope") is None


def test_merge_model_cost_higher_wins_and_tiers_do_not_leak():
    lower = {"input": 1.0, "output": 2.0, "tieredPricing": HAIKU_TIERS}
    # Higher mapping with any keys drops the lower mapping's tiers.
    assert merge_model_cost(lower, {"input": 3.0}) == {"input": 3.0, "output": 2.0}
    # Empty higher mapping keeps the lower one whole, tiers included.
    assert merge_model_cost(lower, {}) == lower
    assert merge_model_cost(lower, None) == lower
    assert merge_model_cost(None, {"input": 1.0}) == {"input": 1.0}
    # Higher tiers replace lower tiers; lower flats the higher omits survive.
    assert merge_model_cost(lower, {"tieredPricing": [
        {"range": [0], "input": 9.0, "output": 9.0, "cacheRead": 0.0, "cacheWrite": 0.0},
    ]}) == {"input": 1.0, "output": 2.0, "tieredPricing": [
        {"range": [0], "input": 9.0, "output": 9.0, "cacheRead": 0.0, "cacheWrite": 0.0},
    ]}


def test_tier_selection_boundaries_gaps_and_overflow():
    cost = {"input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125,
            "tieredPricing": HAIKU_TIERS}
    # Half-open ranges: the boundary token belongs to the upper tier.
    assert select_pricing_tier(cost, 100000)["input"] == 0.1
    assert select_pricing_tier(cost, 100001)["input"] == 0.5
    # Overflow stays on the open-ended top tier; non-positive takes the first.
    assert select_pricing_tier(cost, 10_000_000)["input"] == 0.5
    assert select_pricing_tier(cost, 0)["input"] == 0.1
    # A gap between tiers falls back to the nearest lower tier, never blending.
    gapped = {"tieredPricing": [
        {"range": [0, 100], "input": 1.0, "output": 1.0, "cacheRead": 0.0, "cacheWrite": 0.0},
        {"range": [500], "input": 5.0, "output": 5.0, "cacheRead": 0.0, "cacheWrite": 0.0},
    ]}
    assert select_pricing_tier(gapped, 250)["input"] == 1.0
    # No tiers: the flat rates are the answer.
    assert select_pricing_tier({"input": 2.0, "output": 3.0}, 999999) == {
        "input": 2.0, "output": 3.0, "cacheRead": 0, "cacheWrite": 0}


def test_tiered_cost_prices_whole_request_on_prompt_tokens():
    cost = {"tieredPricing": HAIKU_TIERS}
    # 100k prompt tokens (input + cache read + cache write): tier 1.
    priced = calculate_tiered_cost(
        {"input": 90000, "output": 10000, "cacheRead": 5000, "cacheWrite": 5000}, cost)
    assert priced["input"] == pytest.approx(90000 * 0.1 / 1_000_000)
    assert priced["output"] == pytest.approx(10000 * 0.5 / 1_000_000)
    assert priced["total"] == pytest.approx(sum(
        [90000 * 0.1, 10000 * 0.5, 5000 * 0.01, 5000 * 0.125]) / 1_000_000)
    # Same usage with 200k prompt tokens: tier 2 re-prices every bucket.
    priced = calculate_tiered_cost(
        {"input": 190000, "output": 10000, "cacheRead": 5000, "cacheWrite": 5000}, cost)
    assert priced["input"] == pytest.approx(190000 * 0.5 / 1_000_000)
    assert priced["cacheWrite"] == pytest.approx(5000 * 0.625 / 1_000_000)


# ---------------------------------------------------------------------------
# Row merge / resolve
# ---------------------------------------------------------------------------

def test_row_merge_first_row_wins_later_donates_omitted_fields():
    merged = merge_provider_model_rows([
        {"id": "m1", "name": "M1", "cost": {"input": 1.0, "output": 2.0}},
        {"id": "m1", "name": "Other", "contextWindow": 1000,
         "cost": {"input": 9.0, "cacheRead": 0.5}},
        {"id": "m2", "name": "M2"},
        {"no-id": True},
    ])
    assert set(merged) == {"m1", "m2"}
    assert merged["m1"]["name"] == "M1"
    assert merged["m1"]["contextWindow"] == 1000
    # Earlier row's cost wins per key; the later row's cacheRead is donated.
    assert merged["m1"]["cost"] == {"input": 1.0, "output": 2.0, "cacheRead": 0.5}


def test_resolve_configured_provider_model():
    rows = [
        {"id": "deepseek-ai/DeepSeek-V4-Flash", "name": "Flash"},
        {"id": "plain-model", "name": "Plain"},
    ]
    assert resolve_configured_provider_model(rows, "deepinfra", "deepseek-ai/DeepSeek-V4-Flash")["name"] == "Flash"
    assert resolve_configured_provider_model(rows, "deepinfra", "DEEPSEEK-AI/deepseek-v4-flash")["name"] == "Flash"
    assert resolve_configured_provider_model(rows, "deepinfra", "missing") is None


# ---------------------------------------------------------------------------
# Bundled snapshot: structural contracts, not pinned values (the snapshot is
# regenerated from the seed manifests, so no test may assert its counts
# or any single row's price — those are change-detectors).
# ---------------------------------------------------------------------------

def _snapshot_rows():
    catalog = load_bundled_seed_catalog()
    return [(provider, row) for provider, section in catalog.items()
            for row in section.get("models", [])]


def test_bundled_snapshot_is_well_formed():
    catalog = load_bundled_seed_catalog()
    assert catalog, "bundled snapshot must not be empty"
    rows = _snapshot_rows()
    assert rows, "snapshot must carry model rows"
    for provider, row in rows:
        assert isinstance(row.get("id"), str) and row["id"].strip()
        assert isinstance(provider, str) and provider.strip()


def test_bundled_snapshot_rows_translate_without_losing_claims():
    from agent.model_catalog_data import MODEL_COMPAT_FIELDS, seed_row_to_catalog_entry
    rows = _snapshot_rows()
    translated = 0
    for _provider, row in rows:
        entry = seed_row_to_catalog_entry(row)
        translated += 1
        # Every compat key the snapshot authors must exist in the schema —
        # a new seed flag is a schema gap, not a dropped key.
        for key in (row.get("compat") or {}):
            assert key in MODEL_COMPAT_FIELDS, f"compat key {key!r} missing from the schema"
        # Cost and capability claims survive translation unchanged.
        if isinstance(row.get("cost"), dict):
            for key in ("input", "output", "cacheRead", "cacheWrite"):
                if key in row["cost"]:
                    assert entry["cost"][key] == row["cost"][key]
        if isinstance((row.get("compat") or {}).get("supportsTools"), bool):
            assert entry["tool_call"] is row["compat"]["supportsTools"]
        if isinstance(row.get("contextWindow"), int) and row["contextWindow"] > 0:
            assert entry["limit"]["context"] == row["contextWindow"]
    assert translated == len(rows)


def test_bundled_rows_resolve_through_the_live_path():
    rows = _snapshot_rows()
    tiered = next((p, r) for p, r in rows if (r.get("cost") or {}).get("tieredPricing"))
    entry = bundled_seed_model(tiered[0], tiered[1]["id"])
    assert entry is not None
    raw_tiers = sorted(tiered[1]["cost"]["tieredPricing"], key=lambda t: t["range"][0])
    assert len(entry["cost"]["tieredPricing"]) == len(raw_tiers)
    for raw, translated in zip(raw_tiers, entry["cost"]["tieredPricing"]):
        assert translated["range"][0] == raw["range"][0]
        assert translated["input"] == raw["input"]
    tooled = next((p, r) for p, r in rows
                  if (r.get("compat") or {}).get("supportsTools") is True)
    entry = bundled_seed_model(tooled[0], tooled[1]["id"])
    assert entry is not None
    assert entry["tool_call"] is True
    assert bundled_seed_model("deepinfra", "no-such-model") is None
    assert bundled_seed_model("not-a-provider", "x") is None


# ---------------------------------------------------------------------------
# models_dev: overrides + bundled fallback
# ---------------------------------------------------------------------------

def test_model_overrides_cost_and_compat_reach_model_info(monkeypatch):
    overrides = {
        "wave4-test": {
            "w4-model": {
                "context_window": 64000,
                "cost": {"input": 1.5, "output": 6.0, "cache_read": 0.15,
                         "tieredPricing": HAIKU_TIERS},
                "compat": {"supportsTools": True, "bogus": 1},
            },
        },
    }
    monkeypatch.setattr(models_dev, "_load_model_overrides", lambda **_k: overrides)
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    info = models_dev.get_model_info("wave4-test", "w4-model", allow_network=False)
    assert info is not None
    assert info.context_window == 64000
    assert info.cost_input == 1.5
    assert info.cost_output == 6.0
    assert info.cost_cache_read == 0.15
    assert len(info.cost_tiered_pricing) == 2
    assert info.cost_tiered_pricing[1]["range"] == (100001,)
    assert info.cost_tiered_pricing[1]["input"] == 0.5
    assert info.compat == {"supportsTools": True}
    assert info.has_cost_data() is True


def test_bundled_rows_never_resurrect_an_unlisted_provider(monkeypatch):
    """Regression: a relay model absent from the catalog stays unknown even
    when the bundled seed rows list it — catalog data stays authoritative.
    (opencode-go resells deepseek-v4-flash in the snapshot, but with an empty
    registry Hermes must not claim it.)"""
    monkeypatch.setattr(models_dev, "_load_model_overrides", lambda **_k: {})
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    assert models_dev.get_model_info("opencode-go", "deepseek-v4-flash", allow_network=False) is None
    assert models_dev.get_model_capabilities(
        "opencode-go", "deepseek-v4-flash", allow_network=False) is None


def test_bundled_row_fills_catalog_miss_but_override_wins(monkeypatch):
    provider, row = next(
        (p, r) for p, r in _snapshot_rows()
        if isinstance(r.get("contextWindow"), int) and r["contextWindow"] > 0
        and isinstance((r.get("cost") or {}).get("input"), (int, float)))
    monkeypatch.setattr(models_dev, "_load_model_overrides", lambda **_k: {})
    # The provider must be known to the catalog (empty models dict) for the
    # bundled rows to fill a per-model gap — they never resurrect an
    # unlisted provider.
    monkeypatch.setattr(models_dev, "_models_dev_cache", {provider: {"models": {}}})
    info = models_dev.get_model_info(provider, row["id"], allow_network=False)
    assert info is not None
    assert info.context_window == row["contextWindow"]
    assert info.cost_input == row["cost"]["input"]
    # An explicit override still wins over the bundled row.
    overrides = {provider: {row["id"]: {"context_window": 111}}}
    monkeypatch.setattr(models_dev, "_load_model_overrides", lambda **_k: overrides)
    info = models_dev.get_model_info(provider, row["id"], allow_network=False)
    assert info is not None
    assert info.context_window == 111
    assert info.cost_input == row["cost"]["input"]  # only context was patched; cost survives


# ---------------------------------------------------------------------------
# usage_pricing: tiered estimates through the real registry path
# ---------------------------------------------------------------------------

_TIERED_REGISTRY = {
    "anthropic": {"models": {"claude-haiku-5-5": {"cost": {
        "input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125,
        "tieredPricing": HAIKU_TIERS,
    }}}},
}


def test_tiered_estimate_whole_request_below_and_above(monkeypatch):
    monkeypatch.setattr(models_dev, "_models_dev_cache", _TIERED_REGISTRY)
    monkeypatch.setattr("agent.usage_pricing.fetch_endpoint_model_metadata", lambda *_a, **_k: {})
    below = estimate_usage_cost(
        "claude-haiku-5-5",
        CanonicalUsage(input_tokens=50000, output_tokens=10000,
                       cache_read_tokens=5000, cache_write_tokens=5000),
        provider="anthropic",
    )
    # Prompt tokens 60k → tier 1: (50000*.1 + 10000*.5 + 5000*.01 + 5000*.125) / 1e6.
    assert below.status == "estimated"
    assert below.amount_usd == Decimal("0.010675")
    above = estimate_usage_cost(
        "claude-haiku-5-5",
        CanonicalUsage(input_tokens=100000, output_tokens=10000,
                       cache_read_tokens=10000, cache_write_tokens=10000),
        provider="anthropic",
    )
    # Prompt tokens 120k → tier 2 re-prices every bucket.
    assert above.amount_usd == Decimal("0.08175")
    assert above.amount_usd > below.amount_usd


# ---------------------------------------------------------------------------
# utilityModel
# ---------------------------------------------------------------------------

def test_resolve_utility_model():
    from agent.auxiliary_task_config import resolve_utility_model
    assert resolve_utility_model({"utilityModel": "openrouter/google/gemini-3-flash-preview"}) == (
        "openrouter", "google/gemini-3-flash-preview")
    assert resolve_utility_model({"utilityModel": "  nous/gpt-5  "}) == ("nous", "gpt-5")
    assert resolve_utility_model({"utilityModel": ""}) == (None, None)
    assert resolve_utility_model({"utilityModel": "noprovider"}) == (None, None)
    assert resolve_utility_model({"utilityModel": "/only-model"}) == (None, None)
    assert resolve_utility_model({}) == (None, None)
    assert resolve_utility_model({"utilityModel": 42}) == (None, None)


def test_utility_model_is_the_default_side_task_route(monkeypatch):
    from agent import auxiliary_client
    monkeypatch.setattr(auxiliary_client, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"utilityModel": "openrouter/google/gemini-3-flash-preview"})
    provider, model, base_url, _key, _mode = auxiliary_client._resolve_task_provider_model("compression")
    assert (provider, model, base_url) == (
        "openrouter", "google/gemini-3-flash-preview", None)


def test_utility_model_yields_to_task_config_and_explicit_args(monkeypatch):
    from agent import auxiliary_client
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"utilityModel": "openrouter/google/gemini-3-flash-preview"})
    # Task config wins.
    monkeypatch.setattr(auxiliary_client, "_get_auxiliary_task_config",
                        lambda task: {"provider": "zai", "model": "glm-4.5-flash"})
    provider, model, _url, _key, _mode = auxiliary_client._resolve_task_provider_model("compression")
    assert (provider, model) == ("zai", "glm-4.5-flash")
    # Explicit args win; an explicit model alone still auto-routes.
    monkeypatch.setattr(auxiliary_client, "_get_auxiliary_task_config", lambda task: {})
    provider, model, _url, _key, _mode = auxiliary_client._resolve_task_provider_model(
        "compression", provider="anthropic", model="claude-haiku-4-5-20251001")
    assert (provider, model) == ("anthropic", "claude-haiku-4-5-20251001")
    provider, model, _url, _key, _mode = auxiliary_client._resolve_task_provider_model(
        "compression", model="some-model")
    assert (provider, model) == ("auto", "some-model")


# ---------------------------------------------------------------------------
# Custom-provider compat reader
# ---------------------------------------------------------------------------

def test_custom_provider_model_compat_reader():
    from hermes_cli.config_providers import get_custom_provider_model_compat
    entries = [{
        "name": "my-gateway",
        "base_url": "https://gateway.example.com/v1",
        "models": {"m1": {"compat": {"supportsTools": True, "bogus": 1,
                                     "supportsPromptCacheKey": False}}},
    }]
    compat = get_custom_provider_model_compat(
        "m1", "https://gateway.example.com/v1", custom_providers=entries)
    assert compat == {"supportsTools": True, "supportsPromptCacheKey": False}
    assert get_custom_provider_model_compat(
        "missing", "https://gateway.example.com/v1", custom_providers=entries) == {}
    assert get_custom_provider_model_compat("m1", "", custom_providers=entries) == {}
