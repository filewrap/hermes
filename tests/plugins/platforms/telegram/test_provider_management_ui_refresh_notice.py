"""Live-catalog refresh notice: it may claim cached models only when the
disk cache holds a real live catalog for the provider.

A failed refresh used to say "showing cached models" unconditionally — a
lie for a provider whose live list was never fetched (AgentRouter: the probe
failed and the entries shown were the provider's static declared models).
"""

from urllib.error import HTTPError

import pytest

from tests.plugins.platforms.telegram.test_provider_management_ui import _live_catalog_harness


@pytest.mark.asyncio
async def test_refresh_notice_claims_cached_models_only_when_a_real_cache_exists(tmp_path, monkeypatch):
    """A failed refresh says "cached models" only when the disk cache holds
    a real live catalog for the provider; with no cache it names the
    provider's listed models instead."""
    from hermes_cli.models import _cache_entry, _credential_fingerprint, _store_cache_entry

    def refused(request, timeout):
        raise HTTPError("https://openrouter.ai/api/v1/models", 401, "Unauthorized", None, None)

    _adapter, ui, source, state = _live_catalog_harness(tmp_path, monkeypatch, refused)
    _store_cache_entry(
        "openrouter",
        _cache_entry(_credential_fingerprint("openrouter"), ["vendor/model-a", "vendor/model-b"]),
    )
    await ui._refresh_live_catalog(state, source)
    assert "Live catalog refresh failed" in state["notice"]
    assert "showing cached models" in state["notice"]
    assert "listed models" not in state["notice"]
