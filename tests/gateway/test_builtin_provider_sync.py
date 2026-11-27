"""Built-in provider sync: a key added anywhere must land in the global store.

Sync means sync — not "custom providers only". A built-in key (Codex,
OpenRouter, ...) added from a group profile must appear in the DM, and a
pre-fix key already sitting in a group profile's store must be healed into
the global pool so the DM sees it. Profile-local entries that shadow a
global grant stay put.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session import SessionSource

OWNER = "111"
CODEX_KEY = "codex-group-key-1"
CODEX_KEY_2 = "codex-dm-key-1"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Launch home + one gc profile home, HERMES_HOME pointed at launch."""
    import gateway.run as gateway_run

    launch = tmp_path / "launch"
    gc = launch / "profiles" / "gc-999"
    launch.mkdir()
    gc.mkdir(parents=True)
    (launch / ".env").write_text(f"OWNER_TELEGRAM_ID={OWNER}\n", encoding="utf-8")
    (launch / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    (gc / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(gateway_run, "_hermes_home", launch)
    return SimpleNamespace(launch=launch, gc=gc)


@pytest.fixture(autouse=True)
def _reset_builtin_heal():
    """Let the once-per-home heal run again for this test's home."""
    import hermes_cli.auth_oauth_grants as heal_mod

    healed = getattr(heal_mod, "_BUILTIN_CREDENTIAL_HEAL_DONE_HOMES", None)
    if isinstance(healed, set):
        healed.clear()
    yield
    if isinstance(healed, set):
        healed.clear()


def _pool_entries(home, provider):
    path = home / "auth.json"
    if not path.exists():
        return []
    store = json.loads(path.read_text(encoding="utf-8"))
    pool = store.get("credential_pool") or {}
    entries = pool.get(provider) or []
    return list(entries) if isinstance(entries, list) else []


def _seed_profile_key(home, provider, key_id, api_key, added_by=OWNER):
    path = home / "auth.json"
    store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    pool = store.setdefault("credential_pool", {})
    pool[provider] = [{
        "id": key_id,
        "label": f"{provider} key",
        "auth_type": "api_key",
        "access_token": api_key,
        "added_by": added_by,
    }]
    path.write_text(json.dumps(store), encoding="utf-8")


def test_builtin_key_add_from_group_scope_lands_in_global_store(homes):
    """A built-in key added under a group scope must land in the launch store."""
    from agent.runtime_scope import bind_profile_scope
    from hermes_cli import auth as auth_mod
    from hermes_cli.auth_commands import add_api_key_credential_for_ui

    with bind_profile_scope(homes.gc, hydrate_secrets=False):
        add_api_key_credential_for_ui("openai-codex", CODEX_KEY, added_by=OWNER)

    assert _pool_entries(homes.launch, "openai-codex"), "key never reached the global store"
    assert _pool_entries(homes.launch, "openai-codex")[0]["added_by"] == OWNER
    assert _pool_entries(homes.gc, "openai-codex") == []

    # The DM (launch scope) reads it, and a group turn sees it via the
    # global-root fallback in read_credential_pool.
    assert auth_mod.read_credential_pool("openai-codex")
    with bind_profile_scope(homes.gc, hydrate_secrets=False):
        from agent.credential_pool import load_pool

        assert [e.id for e in load_pool("openai-codex").entries()]


def test_heal_moves_profile_codex_key_to_global(homes):
    """A pre-fix group-stored Codex key moves to the global pool on first load."""
    from agent.credential_pool import load_pool
    from hermes_cli import auth as auth_mod

    _seed_profile_key(homes.gc, "openai-codex", "seed-1", CODEX_KEY)

    entries = load_pool("openai-codex").entries()
    assert [e.id for e in entries] == ["seed-1"]

    moved = _pool_entries(homes.launch, "openai-codex")
    assert len(moved) == 1
    assert moved[0]["id"] == "seed-1"
    assert moved[0]["added_by"] == OWNER
    assert _pool_entries(homes.gc, "openai-codex") == []
    assert auth_mod.read_credential_pool("openai-codex")


def test_heal_keeps_shadow_when_global_has_entries(homes):
    """When the global pool already has a key, the profile's key stays put."""
    from agent.credential_pool import load_pool

    _seed_profile_key(homes.launch, "openai-codex", "global-1", CODEX_KEY_2)
    _seed_profile_key(homes.gc, "openai-codex", "profile-1", CODEX_KEY)

    load_pool("openai-codex")

    assert [e["id"] for e in _pool_entries(homes.launch, "openai-codex")] == ["global-1"]
    assert [e["id"] for e in _pool_entries(homes.gc, "openai-codex")] == ["profile-1"]


def test_heal_skips_custom_and_unknown_keys(homes):
    """Custom and unknown pool keys are not the heal's business."""
    from agent.credential_pool import load_pool

    _seed_profile_key(homes.gc, "custom:acme", "custom-1", "acme-key")
    _seed_profile_key(homes.gc, "definitely-not-a-provider", "unknown-1", "x")

    load_pool("openai-codex")

    assert [e["id"] for e in _pool_entries(homes.gc, "custom:acme")] == ["custom-1"]
    assert [e["id"] for e in _pool_entries(homes.gc, "definitely-not-a-provider")] == ["unknown-1"]
    assert _pool_entries(homes.launch, "openai-codex") == []
