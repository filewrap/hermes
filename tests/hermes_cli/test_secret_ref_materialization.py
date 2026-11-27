"""SecretRef materialisation at the credential-resolver seams.

Behaviour contracts: a provider ``api_key`` given as a SecretRef dict resolves
through the declared provider; a literal passes through; an unresolvable ref
raises in strict mode instead of silently falling through to another
credential (``key_env`` must never stand in for a declared ref).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.secret_ref import UnresolvedSecretInputError
from hermes_cli.fallback_config import resolve_entry_api_key
from hermes_cli.runtime_provider_custom import _entry_credential, _match_new_style_provider


@pytest.fixture
def scoped(monkeypatch):
    import agent.secret_scope as ss

    token = ss.set_secret_scope({"MY_KEY": "sekrit", "OTHER_KEY": "other-sekrit"})
    try:
        yield
    finally:
        ss.reset_secret_scope(token)


def test_resolve_entry_api_key_materialises_ref(scoped):
    assert resolve_entry_api_key(
        {"api_key": {"source": "env", "provider": "default", "id": "MY_KEY"}}) == "sekrit"
    assert resolve_entry_api_key({"api_key": "literal"}) == "literal"
    assert resolve_entry_api_key({"api_key": "$MY_KEY"}) == "sekrit"
    assert resolve_entry_api_key({"key_env": "MY_KEY"}) == "sekrit"
    assert resolve_entry_api_key({}) is None


def test_resolve_entry_api_key_strict_raises_instead_of_falling_through(scoped):
    with pytest.raises(UnresolvedSecretInputError):
        resolve_entry_api_key(
            {"api_key": {"source": "env", "provider": "default", "id": "ABSENT_KEY"},
             "key_env": "OTHER_KEY"})


def test_entry_credential_materialises_ref(scoped):
    rp = SimpleNamespace(has_usable_secret=lambda c: isinstance(c, str) and len(c or "") >= 4)
    assert _entry_credential(
        rp, {"api_key": {"source": "env", "provider": "default", "id": "MY_KEY"}}, "x") == "sekrit"
    assert _entry_credential(rp, {"api_key": "literal-key"}, "x") == "literal-key"
    assert _entry_credential(rp, {"key_env": "MY_KEY"}, "x") == "sekrit"


def test_entry_credential_ref_never_borrows_key_env(scoped):
    rp = SimpleNamespace(has_usable_secret=lambda c: isinstance(c, str) and len(c or "") >= 4)
    with pytest.raises(UnresolvedSecretInputError):
        _entry_credential(
            rp,
            {"api_key": {"source": "env", "provider": "default", "id": "ABSENT_KEY"},
             "key_env": "OTHER_KEY"},
            "x")


def test_match_new_style_provider_passes_ref_through_when_not_resolving(scoped):
    ref = {"source": "env", "provider": "default", "id": "MY_KEY"}
    providers = {"mine": {"name": "mine", "base_url": "https://api.example/v1", "api_key": ref}}
    result, _key = _match_new_style_provider("mine", providers, resolve_credentials=False)
    assert result["api_key"] is ref  # untouched for later resolution, never stringified
    result, _key = _match_new_style_provider("mine", providers, resolve_credentials=True)
    assert result["api_key"] == "sekrit"
