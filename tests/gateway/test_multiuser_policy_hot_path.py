from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gateway import mongo_multiuser


def test_policy_cache_reuses_only_identical_profile_policy_and_still_reads_every_time(
    tmp_path, monkeypatch
):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    env_a = home_a / ".env"
    env_b = home_b / ".env"
    env_a.write_bytes(
        b"OWNER_TELEGRAM_ID=101\nTELEGRAM_ALLOWED_USERS=101,202\nOPENROUTER_API_KEY=secret-a\n"
    )
    env_b.write_bytes(
        b"OWNER_TELEGRAM_ID=303\nTELEGRAM_ALLOWED_USERS=303\nOPENROUTER_API_KEY=secret-b\n"
    )

    from agent import secret_scope

    parse_env_text = secret_scope._parse_env_text
    parse_calls = []

    def counted_parse(text):
        parse_calls.append(text)
        return parse_env_text(text)

    monkeypatch.setattr(secret_scope, "_parse_env_text", counted_parse)
    read_bytes = Path.read_bytes
    reads = []

    def counted_read(path):
        if path.name == ".env":
            reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)

    assert mongo_multiuser.enabled(home_a)
    assert mongo_multiuser.owner_user_id(home_a) == "101"
    store = mongo_multiuser.store_for_home(home_a)
    assert store.owner_user_id == "101"
    assert len(parse_calls) == 1
    assert len(reads) == 3

    original_stat = env_a.stat()
    replacement = (
        b"OWNER_TELEGRAM_ID=404\nTELEGRAM_ALLOWED_USERS=404,505\nOPENROUTER_API_KEY=secret-x\n"
    )
    assert len(replacement) == env_a.stat().st_size
    env_a.write_bytes(replacement)
    os.utime(env_a, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert mongo_multiuser.owner_user_id(home_a) == "404"
    assert len(parse_calls) == 2

    assert mongo_multiuser.owner_user_id(home_b) == "303"
    assert mongo_multiuser.owner_user_id(home_a) == "404"
    assert len(parse_calls) == 3
    assert len(reads) == 6
    cached_entries = list(mongo_multiuser._POLICY_CACHE.values())
    cached = repr(cached_entries)
    assert all(set(policy) <= {"OWNER_TELEGRAM_ID", "TELEGRAM_ALLOWED_USERS"}
               for _, policy in cached_entries)
    assert not any(secret in cached for secret in ("secret-a", "secret-b", "secret-x"))


def test_policy_missing_unreadable_overlay_conflicts_and_live_bans(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    env_path = home / ".env"
    runtime_key = mongo_multiuser._HF_RUNTIME_POLICY
    runtime = {
        "home": str(home),
        "policy": {"OWNER_TELEGRAM_ID": "100", "TELEGRAM_ALLOWED_USERS": "100,200"},
    }
    monkeypatch.setenv(runtime_key, json.dumps(runtime))

    assert mongo_multiuser.owner_user_id(home) == "100"
    env_path.write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100,200\n")
    assert mongo_multiuser.owner_user_id(home) == "100"
    env_path.write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100,300\n")
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USERS conflict"):
        mongo_multiuser.owner_user_id(home)
    env_path.unlink()
    assert mongo_multiuser.owner_user_id(home) == "100"

    runtime["policy"]["OWNER_TELEGRAM_ID"] = "300"
    monkeypatch.setenv(runtime_key, json.dumps(runtime))
    assert mongo_multiuser.owner_user_id(home) == "300"
    runtime["policy"]["OWNER_TELEGRAM_ID"] = "100"
    monkeypatch.setenv(runtime_key, json.dumps(runtime))

    real_read_bytes = Path.read_bytes

    def unreadable(path):
        if path == env_path:
            raise PermissionError("denied")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(RuntimeError, match="Cannot read multiuser bootstrap policy"):
        mongo_multiuser.enabled(home)
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
    monkeypatch.delenv(runtime_key)
    assert not mongo_multiuser.enabled(home)

    env_path.write_text("OWNER_TELEGRAM_ID=100\nTELEGRAM_ALLOWED_USERS=100,200\n")
    store = mongo_multiuser.store_for_home(home)
    from agent.mongo_tenants import Principal

    store.ban_user(Principal("100"), "200", True)
    access = mongo_multiuser.store_for_home(home).access_for_scope("200", "dm", "200")
    assert access["banned"] is True and access["allowed"] is False
    store.ban_user(Principal("100"), "200", False)
    access = mongo_multiuser.store_for_home(home).access_for_scope("200", "dm", "200")
    assert access["banned"] is False and access["allowed"] is True
