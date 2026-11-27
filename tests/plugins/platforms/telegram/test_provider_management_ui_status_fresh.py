"""Fresh credential status on every providers/catalog render (live bugs 2026-11-24).

- The providers list and the model catalogs list re-read the credential pool
  on every render: adding or removing a credential flips the row's
  Added/Missing without reopening the card (no stale snapshot survives
  navigation).
- Aliased providers (kilo -> kilocode) read their credentials under the
  canonical pool key, the same key the add flow stores them under.
- The summary "N added" counts providers whose status is Added, from the
  same fresh read.
- Catalog/model-list headers put each fact on its own line, and model
  buttons drop the vendor prefix so siblings stay distinguishable.
- The cached-model freshness line shows the last-updated time
  ("Updated 2h ago") or is omitted; "freshness not verified" is gone.
"""

import time

import pytest

from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
from tests.plugins.platforms.telegram.test_provider_management_ui import (
    CallbackEvent, FakeAdapter, Source, _plain_rich_text, _provider, _state,
)


def _labels(markup):
    return [
        button.text
        for row in markup.rows
        for button in row.buttons
    ]


def _seed_pool(monkeypatch, tmp_path, slug, entry_id="entry-1"):
    """Store one API-key credential for *slug* in the temp home's pool."""
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool(slug)
    pool.add_entry(PooledCredential(
        provider=slug, id=entry_id, label="Credential 1", auth_type="api_key",
        priority=0, source="manual", access_token="sk-test-credential",
    ))
    return pool


def _providers_view_state(ui, providers, **kwargs):
    kwargs.setdefault("profile_home", None)
    return ui._new_state(
        Source(), providers, current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", view="providers",
        **kwargs,
    )


# ── Bug 2: the catalog shows Missing for every provider ───────────────


def test_catalog_rows_show_added_from_the_live_pool(tmp_path, monkeypatch):
    """Snapshot rows carry no "ready" key; the catalog must still read the
    status from the live pool, like /providers does."""
    _seed_pool(monkeypatch, tmp_path, "openrouter")
    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [
        _provider(slug="openrouter", name="OpenRouter", is_current=True),
        _provider(slug="nous", name="Nous Portal", is_current=False),
    ]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=tmp_path,
        view="models",
    )
    _text, markup = ui._render(state)
    labels = [label for label, data in
              [(b.text, b.type.data.decode("utf-8")) for row in markup.rows for b in row.buttons]
              if ":models:" in data]
    assert any(label == "Current · OpenRouter · 2 · Added" for label in labels)
    assert any(label == "Nous Portal · 2 · Missing" for label in labels)


# ── Bug 4: the status goes stale on navigation ─────────────────────────


def test_providers_list_rereads_the_pool_on_every_render(tmp_path, monkeypatch):
    """Adding then removing a credential flips the same card's row on the
    next render — the render never trusts the ready flag it rendered last
    time."""
    pool = _seed_pool(monkeypatch, tmp_path, "openrouter")
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = _providers_view_state(
        ui, [_provider(slug="openrouter", name="OpenRouter", is_current=True)],
        profile_home=tmp_path,
    )
    _text, markup = ui._render(state)
    assert "Current · OpenRouter · Added" in _labels(markup)

    pool.remove_index(1)
    _text, markup = ui._render(state)
    assert "Current · OpenRouter · Missing" in _labels(markup)

    _seed_pool(monkeypatch, tmp_path, "openrouter", entry_id="entry-2")
    _text, markup = ui._render(state)
    assert "Current · OpenRouter · Added" in _labels(markup)


# ── Bug 3: kilo shows Missing although its token is added ─────────────


def test_kilo_reads_credentials_stored_under_kilocode(tmp_path, monkeypatch):
    """The add flow stores kilo's key under the canonical pool key
    "kilocode"; the row keeps the "kilo" slug, so the read must check the
    canonical key too."""
    _seed_pool(monkeypatch, tmp_path, "kilocode")
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = _providers_view_state(
        ui, [_provider(slug="kilo", name="Kilo", is_current=True)],
        profile_home=tmp_path,
    )
    _text, markup = ui._render(state)
    assert "Current · Kilo · Added" in _labels(markup)


def test_summary_added_count_counts_ready_providers(tmp_path, monkeypatch):
    """The summary counts providers whose status is Added from the same
    fresh read — not the number of non-catalog providers."""
    _seed_pool(monkeypatch, tmp_path, "openrouter")
    _seed_pool(monkeypatch, tmp_path, "nous", entry_id="entry-nous")
    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [
        _provider(slug="openrouter", name="OpenRouter", is_current=True),
        _provider(slug="nous", name="Nous Portal", is_current=False),
        _provider(slug="nvidia", name="Nvidia", is_current=False),
    ]
    state = _providers_view_state(ui, providers, profile_home=tmp_path)
    rendered, _markup = ui._render(state)
    assert "<p>3 providers · 2 added</p>" in rendered


# ── Bug 1: catalog alignment ───────────────────────────────────────────


def test_catalogs_list_puts_each_fact_on_its_own_line():
    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0)
                 for i in range(3)]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a", current_provider="p0",
        session_key="s", profile_home=None, view="models",
    )
    text, _markup = ui._render(state)
    assert "Route: **`p0 / vendor/model-a`**\n\nProfile-wide default:" in text
    assert "Profile-wide default: **" in text
    assert "\n\nProviders with models: **3**" in text


def test_per_provider_model_list_puts_each_fact_on_its_own_line():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=None,
        view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Entries: **2** · matching: **2**\n\nGreen marks the current model" in text
    assert "Route: **`openrouter / vendor/model-a`**\n\nProfile-wide default:" in text
    assert "freshness not verified" not in text


# ── Model buttons: indistinguishable truncated labels ──────────────────


def _model_button_labels(ui, state):
    _text, markup = ui._render(state)
    return {
        button.type.data.decode("utf-8"): button.text
        for row in markup.rows
        for button in row.buttons
        if ":select:" in button.type.data.decode("utf-8")
    }


def test_model_buttons_drop_the_vendor_prefix_and_stay_distinct():
    ui = TelegramProviderManagementUI(FakeAdapter())
    models = ["anthropic/claude-fable-5.2", "anthropic/claude-fable-5.3"]
    state = ui._new_state(
        Source(), [_provider(models=models, total_models=2)],
        current_model=models[0], current_provider="openrouter",
        session_key="s", profile_home=None, view="models",
        selected_provider="openrouter",
    )
    labels = _model_button_labels(ui, state)
    assert labels[f"pv:{state['id']}:select:0"] == "Current · claude-fable-5.2"
    assert labels[f"pv:{state['id']}:select:1"] == "claude-fable-5.3"


def test_model_buttons_fall_back_to_the_full_id_on_collision():
    """Two models from different vendors with the same name must still read
    differently after the vendor prefix is dropped. The vendors share a
    23-char prefix, so the old front truncation hid the difference too."""
    ui = TelegramProviderManagementUI(FakeAdapter())
    models = ["aaaaaaaaaaaaaaaaaaaaaaa-x/shared-model",
              "aaaaaaaaaaaaaaaaaaaaaaa-y/shared-model"]
    state = ui._new_state(
        Source(), [_provider(models=models, total_models=2)],
        current_model=models[0], current_provider="openrouter",
        session_key="s", profile_home=None, view="models",
        selected_provider="openrouter",
    )
    labels = _model_button_labels(ui, state)
    shown = list(labels.values())
    assert len(set(shown)) == len(shown)
    assert any("-x/shared" in label for label in shown)
    assert any("-y/shared" in label for label in shown)


# ── Model cache: last-updated instead of "freshness not verified" ─────


def test_per_provider_catalog_shows_last_updated_time():
    ui = TelegramProviderManagementUI(FakeAdapter())
    provider = _provider(catalog_updated_at=time.time() - 7200)
    state = ui._new_state(
        Source(), [provider], current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=None,
        view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Updated 2h ago" in text
    assert "freshness not verified" not in text


def test_per_provider_catalog_omits_freshness_without_a_timestamp():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=None,
        view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Freshness:" not in text
    assert "freshness not verified" not in text


def test_provider_detail_shows_last_updated_time():
    ui = TelegramProviderManagementUI(FakeAdapter())
    provider = _provider(
        catalog_updated_at=time.time() - 7200, catalog_status="cached_or_curated",
    )
    state = ui._new_state(
        Source(), [provider], current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=None,
        view="provider", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Freshness: Updated 2h ago" in _plain_rich_text(text)
    assert "freshness not verified" not in _plain_rich_text(text)


# ── Refresh writes the cache ───────────────────────────────────────────


class _FakeRunner:
    def __init__(self, rows, order):
        self.rows = rows
        self.order = order

    async def _provider_inventory_snapshot(self, source, *, force_rebuild=False):
        self.order.append("snapshot")
        return (self.rows, "vendor/model-a", "openrouter", "session-1", None)


@pytest.mark.asyncio
async def test_refresh_live_catalog_force_refreshes_the_cache_first(monkeypatch):
    """An explicit refresh writes the fresh list into the disk cache BEFORE
    the snapshot rebuild, so the rebuilt rows serve the fresh list."""
    order = []
    rows = [_provider(ready=True)]
    adapter = FakeAdapter()
    adapter.gateway_runner = _FakeRunner(rows, order)
    ui = TelegramProviderManagementUI(adapter)
    ui._load_credentials = lambda _p: [{
        "id": "safe-id", "auth_type": "api_key", "priority": 0,
        "status": "ok", "active": True,
    }]
    ui._has_local_auth_material = lambda _p: False

    def _fake_cached(slug, *, force_refresh=False, **_kwargs):
        order.append("cache")
        assert force_refresh is True
        return []

    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids", _fake_cached)
    monkeypatch.setattr(
        TelegramProviderManagementUI, "_probe_live_catalog",
        staticmethod(lambda _provider: {
            "attempted": True, "status": 200, "count": 3, "error": None,
        }),
    )
    state = ui._new_state(
        Source(), [_provider(ready=True)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="s", profile_home=None,
        view="provider", selected_provider="openrouter",
    )
    state["message_id"] = "55"
    await ui._refresh_live_catalog(state, Source())
    assert order[0] == "cache"
    assert order[1] == "snapshot"
    assert "Live catalog refresh worked" in state["notice"]


@pytest.mark.asyncio
async def test_providers_summary_line_counts_shared_providers(tmp_path, monkeypatch):
    """The providers summary line counts shared providers; names live on the provider page."""
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool("openrouter")
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="Credential 1", auth_type="api_key",
        priority=0, source="manual", access_token="sk-test",
    )
    entry.extra["added_by"] = "123"
    entry.extra["shared"] = True
    pool.add_entry(entry)

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path,
    )
    state = _state(ui)
    rendered, _markup = ui._render(state)
    assert "<p>1 provider · 1 added · 1 shared</p>" in rendered
    assert "<td><b>Shared</b></td>" not in rendered
