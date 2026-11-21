from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin


def _runner(admins=None):
    runner = object.__new__(GatewaySlashCommandsMixin)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(
        enabled=True, extra=({"allow_admin_from": admins} if admins is not None else {}),
    )})
    runner._resolve_profile_home_for_source = lambda source: "/profiles/" + source.profile
    return runner


def _event(user="alice", profile="satellite"):
    source = SessionSource(platform=Platform.DISCORD, user_id=user, chat_id="c",
                           user_name=user, chat_type="dm", profile=profile)
    return MessageEvent(text="/sync", source=source)


class _CredentialStoreError(Exception):
    pass


def test_sync_refuses_when_no_admin_is_configured(monkeypatch):
    called = []
    monkeypatch.setitem(sys.modules, "agent.mongo_credentials", SimpleNamespace(
        enabled=lambda home: called.append(home), CredentialStoreError=_CredentialStoreError))
    result = asyncio.run(_runner()._handle_sync_command(_event()))
    assert "owner/admin-only" in result
    assert called == []


def test_sync_reports_pending_configuration(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.mongo_credentials", SimpleNamespace(
        enabled=lambda home: False, CredentialStoreError=_CredentialStoreError))
    result = asyncio.run(_runner(["alice"])._handle_sync_command(_event()))
    assert "not configured yet" in result


def test_sync_missing_uri_is_configuration_pending(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.mongo_credentials", SimpleNamespace(
        enabled=lambda home: True, CredentialStoreError=_CredentialStoreError))
    service = ModuleType("gateway.mongo_sync")
    def sync_profile(home):
        raise _CredentialStoreError("MongoDB storage is not configured: mongodb://secret")
    service.sync_profile = sync_profile
    monkeypatch.setitem(sys.modules, "gateway.mongo_sync", service)
    result = asyncio.run(_runner(["alice"])._handle_sync_command(_event()))
    assert "not configured yet" in result
    assert "secret" not in result


def test_sync_uses_routed_profile_and_only_returns_safe_counters(monkeypatch):
    homes = []
    monkeypatch.setitem(sys.modules, "agent.mongo_credentials", SimpleNamespace(
        enabled=lambda home: True, CredentialStoreError=_CredentialStoreError))
    service = ModuleType("gateway.mongo_sync")
    def sync_profile(home):
        homes.append(home)
        return {"counters": {"synced": 4, "skipped": 2}, "filename": "private.txt",
                "error": "mongodb://secret"}
    service.sync_profile = sync_profile
    monkeypatch.setitem(sys.modules, "gateway.mongo_sync", service)

    result = asyncio.run(_runner(["alice"])._handle_sync_command(_event()))
    assert homes == [Path("/profiles/satellite")]
    assert "synced: 4" in result and "skipped: 2" in result
    assert "private.txt" not in result and "secret" not in result


def test_sync_refuses_nonadmin_even_with_an_enabled_policy(monkeypatch):
    called = []
    monkeypatch.setitem(sys.modules, "agent.mongo_credentials", SimpleNamespace(
        enabled=lambda home: called.append(home), CredentialStoreError=_CredentialStoreError))
    result = asyncio.run(_runner(["owner"])._handle_sync_command(_event(user="guest")))
    assert "owner/admin-only" in result
    assert not called


def test_sync_unexpected_exception_never_reaches_chat_or_logs(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "agent.mongo_credentials", SimpleNamespace(
        enabled=lambda home: True, CredentialStoreError=_CredentialStoreError))
    service = ModuleType("gateway.mongo_sync")
    def sync_profile(home):
        raise RuntimeError("mongodb://user:private-password@example.invalid")
    service.sync_profile = sync_profile
    monkeypatch.setitem(sys.modules, "gateway.mongo_sync", service)
    result = asyncio.run(_runner(["alice"])._handle_sync_command(_event()))
    assert "failed" in result
    assert "private-password" not in result + caplog.text
    assert "mongodb://" not in result + caplog.text
