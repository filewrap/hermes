"""SecretRef contract, strict/inspect modes, and provider materialisation.

Behaviour contracts for ``agent/secret_ref.py`` (ported shape): the ref grammar,
``$NAME`` shorthand, strict-vs-inspect reads, and one materialisation per source
(env / store / file / exec), plus the invariant that the ``secrets:`` config
section is read through the config loader the runtime uses.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent.secret_ref import (
    SecretRef,
    SecretResolutionError,
    UnresolvedSecretInputError,
    assert_secret_input_resolved,
    coerce_secret_ref,
    is_secret_ref,
    is_valid_secret_ref,
    materialize_secret_input,
    materialize_secret_ref,
    materialize_secret_refs,
    normalize_and_group_secret_refs,
    parse_env_template_secret_ref,
    resolve_secret_input,
    secret_ref_key,
)


# ── Contract ──────────────────────────────────────────────────────────────────


def test_ref_shape_requires_exactly_three_keys():
    assert is_secret_ref({"source": "env", "provider": "default", "id": "FOO"})
    assert not is_secret_ref({"source": "env", "id": "FOO"})  # no provider
    assert not is_secret_ref({"source": "env", "provider": "default", "id": "FOO", "extra": 1})
    assert not is_secret_ref({"source": "vault", "provider": "default", "id": "FOO"})
    assert not is_secret_ref({"source": "env", "provider": "default", "id": "  "})
    assert not is_secret_ref("env:default:FOO")


def test_ref_validation_matrix():
    assert is_valid_secret_ref(SecretRef("env", "default", "MY_KEY_1"))
    assert is_valid_secret_ref(SecretRef("store", "default", "MY_KEY"))
    assert is_valid_secret_ref(SecretRef("file", "json", "/providers/openai/apiKey"))
    assert is_valid_secret_ref(SecretRef("file", "json", "value"))
    assert is_valid_secret_ref(SecretRef("exec", "vault", "prod/db/password"))
    assert not is_valid_secret_ref(SecretRef("env", "default", "lowercase"))  # env ids are UPPER_SNAKE
    assert not is_valid_secret_ref(SecretRef("env", "Default", "MY_KEY"))  # aliases are lowercase
    assert not is_valid_secret_ref(SecretRef("file", "json", "relative/path"))  # pointer or "value"
    assert not is_valid_secret_ref(SecretRef("exec", "vault", "../escape"))  # no dot segments
    assert secret_ref_key(SecretRef("env", "default", "FOO")) == "env:default:FOO"


def test_env_shorthand_parses_only_dollar_forms():
    assert parse_env_template_secret_ref("$FOO") == SecretRef("env", "default", "FOO")
    assert parse_env_template_secret_ref("${FOO}") == SecretRef("env", "default", "FOO")
    assert parse_env_template_secret_ref("plain-token") is None  # a bare literal is never a ref
    assert parse_env_template_secret_ref("$lowercase") is None
    assert parse_env_template_secret_ref(42) is None


def test_legacy_ref_without_provider_fills_default():
    assert coerce_secret_ref({"source": "env", "id": "FOO"}) == SecretRef("env", "default", "FOO")
    assert coerce_secret_ref({"source": "env", "id": "FOO"}, {"env": "mine"}) == SecretRef("env", "mine", "FOO")


# ── Strict vs inspect ─────────────────────────────────────────────────────────


def test_resolve_secret_input_modes():
    assert resolve_secret_input("lit", path="p").status == "available"
    assert resolve_secret_input("lit", path="p").value == "lit"
    ref = {"source": "env", "provider": "default", "id": "FOO"}
    with pytest.raises(UnresolvedSecretInputError):
        resolve_secret_input(ref, path="p", mode="strict")
    outcome = resolve_secret_input(ref, path="p", mode="inspect")
    assert outcome.status == "configured_unavailable"
    assert outcome.ref == SecretRef("env", "default", "FOO")
    assert outcome.value is None  # the value is never reported
    assert resolve_secret_input(None, path="p").status == "missing"
    assert resolve_secret_input("", path="p").status == "missing"
    with pytest.raises(UnresolvedSecretInputError):
        assert_secret_input_resolved(ref, path="p")
    assert_secret_input_resolved("lit", path="p")


# ── Materialisation ───────────────────────────────────────────────────────────


def test_materialize_env_ref_via_secret_scope():
    import agent.secret_scope as ss

    token = ss.set_secret_scope({"MY_KEY": "sekrit"})
    try:
        assert materialize_secret_input(
            {"source": "env", "provider": "default", "id": "MY_KEY"}, path="p") == "sekrit"
        assert materialize_secret_input("$MY_KEY", path="p") == "sekrit"
        assert materialize_secret_input("literal-token", path="p") == "literal-token"
    finally:
        ss.reset_secret_scope(token)


def test_materialize_env_ref_strict_raises_when_missing():
    import agent.secret_scope as ss

    token = ss.set_secret_scope({})
    try:
        with pytest.raises(UnresolvedSecretInputError):
            materialize_secret_input(
                {"source": "env", "provider": "default", "id": "ABSENT_KEY"}, path="p")
        assert materialize_secret_input(
            {"source": "env", "provider": "default", "id": "ABSENT_KEY"},
            path="p", mode="inspect") is None
    finally:
        ss.reset_secret_scope(token)


def test_materialize_store_ref_reads_profile_env_file(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("STORE_KEY=store-sekrit\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert materialize_secret_ref(SecretRef("store", "default", "STORE_KEY")) == "store-sekrit"
    assert materialize_secret_ref(SecretRef("store", "default", "NO_SUCH_KEY")) is None


def test_materialize_file_ref_json_pointer(tmp_path):
    data = tmp_path / "secrets.json"
    data.write_text(json.dumps({"providers": {"openai": {"apiKey": "sk-file"}}}), encoding="utf-8")
    config = {"secrets": {"providers": {"jsonfile": {"source": "file", "path": str(data)}}}}
    assert materialize_secret_ref(
        SecretRef("file", "jsonfile", "/providers/openai/apiKey"), config=config) == "sk-file"
    with pytest.raises(SecretResolutionError):
        materialize_secret_ref(SecretRef("file", "jsonfile", "/providers/nope"), config=config)


def test_materialize_file_ref_single_value(tmp_path):
    raw = tmp_path / "token.txt"
    raw.write_text("sk-raw\n", encoding="utf-8")
    config = {"secrets": {"providers": {"rawfile": {"source": "file", "path": str(raw), "mode": "singleValue"}}}}
    assert materialize_secret_ref(SecretRef("file", "rawfile", "value"), config=config) == "sk-raw"


def test_materialize_exec_ref_batch_calls_provider_once(tmp_path):
    script = tmp_path / "secret_provider.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "(Path(__file__).parent / 'calls.txt').open('a').write('x')\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'protocolVersion': 1,\n"
        "                  'values': {i: f'value-for-{i}' for i in req['ids']}}))\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    config = {"secrets": {"providers": {"vault": {"source": "exec", "command": str(script)}}}}
    refs = [SecretRef("exec", "vault", "KEY_ONE"), SecretRef("exec", "vault", "KEY_TWO")]
    out = materialize_secret_refs(refs, config=config)
    assert out == {"exec:vault:KEY_ONE": "value-for-KEY_ONE", "exec:vault:KEY_TWO": "value-for-KEY_TWO"}
    assert (tmp_path / "calls.txt").read_text(encoding="utf-8") == "x"  # one call for the batch


def test_exec_ref_rejects_world_writable_command(tmp_path):
    script = tmp_path / "loose.py"
    script.write_text("print('{}')", encoding="utf-8")
    script.chmod(0o777)
    config = {"secrets": {"providers": {"vault": {"source": "exec", "command": str(script)}}}}
    with pytest.raises(SecretResolutionError):
        materialize_secret_ref(SecretRef("exec", "vault", "KEY"), config=config)


def test_provider_source_mismatch_refused(tmp_path):
    data = tmp_path / "secrets.json"
    data.write_text("{}", encoding="utf-8")
    config = {"secrets": {"providers": {"jsonfile": {"source": "file", "path": str(data)}}}}
    with pytest.raises((ValueError, SecretResolutionError)):
        materialize_secret_ref(SecretRef("env", "jsonfile", "FOO"), config=config)
    with pytest.raises(SecretResolutionError):
        materialize_secret_ref(SecretRef("file", "undeclared", "/x"), config=config)


def test_normalize_and_group_dedupes_and_groups():
    refs = [
        SecretRef("env", "default", "A"),
        SecretRef("env", "default", "A"),  # duplicate
        SecretRef("env", "default", "B"),
        SecretRef("file", "json", "/x"),
    ]
    groups = dict(((s, p), g) for s, p, g in normalize_and_group_secret_refs(refs))
    assert {r.id for r in groups[("env", "default")]} == {"A", "B"}
    assert len(groups[("file", "json")]) == 1


# ── Config invariant ──────────────────────────────────────────────────────────


def test_secrets_section_read_through_config_loader(tmp_path, monkeypatch):
    """The ``secrets:`` section registered in DEFAULT_CONFIG is read by the
    runtime loader: a file provider declared in a temp config.yaml resolves
    with no explicit config argument."""
    from hermes_cli.config import read_raw_config

    data = tmp_path / "secrets.json"
    data.write_text(json.dumps({"k": "via-config-loader"}), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  defaults: {env: default, file: default, exec: default, store: default}\n"
        "  providers:\n"
        f"    jsonfile: {{source: file, path: \"{data}\"}}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert read_raw_config().get("secrets", {}).get("providers", {}).get("jsonfile", {}).get("source") == "file"
    assert os.environ.get("HERMES_HOME") == str(tmp_path)
    assert materialize_secret_ref(SecretRef("file", "jsonfile", "/k")) == "via-config-loader"
