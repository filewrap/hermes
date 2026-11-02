"""Focused tests for owner/admin-gated /sync (Mongo profile sync).

/sync is owner/admin-only: the admin gate (``allow_admin_from`` for the source
platform; no admin list means no one may sync) runs before any storage seam
is touched, and any failure is contained as a generic reply so URIs and
credentials never reach chat.
"""
import asyncio
from types import SimpleNamespace
from gateway.slash_commands import GatewaySlashCommandsMixin

OWNER = "6802929470"
SUDO = "8495104954"


def runner_for(home):
    runner = object.__new__(GatewaySlashCommandsMixin)
    runner.config = {}
    runner._resolve_profile_home_for_source = lambda _source: str(home)
    return runner


def _admin_runner(home, admin_ids=(OWNER,)):
    runner = runner_for(home)
    runner.config = SimpleNamespace(platforms={"telegram": {"allow_admin_from": list(admin_ids)}})
    return runner


def _event(user_id):
    return SimpleNamespace(
        source=SimpleNamespace(user_id=user_id, platform="telegram", chat_type="private", profile="default")
    )


def test_disabled_sync_does_not_write(tmp_path, monkeypatch):
    from agent import mongo_credentials
    from gateway import mongo_sync

    # No admin list configured: nobody may sync, and no storage seam is reached.
    monkeypatch.setattr(
        mongo_credentials, "enabled", lambda *_a: (_ for _ in ()).throw(AssertionError("admin gate must run first"))
    )
    monkeypatch.setattr(
        mongo_sync, "sync_profile", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("remote access"))
    )
    reply = asyncio.run(runner_for(tmp_path)._handle_sync_command(_event(OWNER)))
    assert "Sync is owner/admin-only" in reply and "No data was written." in reply


def test_admin_sync_without_credentials_reports_not_configured(tmp_path, monkeypatch):
    from agent import mongo_credentials
    from gateway import mongo_sync

    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: False)
    monkeypatch.setattr(
        mongo_sync, "sync_profile", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("sync must not run unconfigured"))
    )
    reply = asyncio.run(_admin_runner(tmp_path)._handle_sync_command(_event(OWNER)))
    assert "Sync is not configured yet" in reply and "not enabled for this profile" in reply


def test_admin_sync_reports_counters_on_success(tmp_path, monkeypatch):
    from agent import mongo_credentials
    from gateway import mongo_sync

    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: True)
    monkeypatch.setattr(mongo_sync, "sync_profile", lambda _home: {"counters": {"synced": 3, "skipped": 1}})
    reply = asyncio.run(_admin_runner(tmp_path)._handle_sync_command(_event(OWNER)))
    assert "Sync complete for profile default" in reply
    assert "synced: 3" in reply and "skipped: 1" in reply


def test_historical_sudo_user_is_not_promoted_to_owner(tmp_path, monkeypatch):
    from agent import mongo_credentials
    from gateway import mongo_sync

    monkeypatch.setattr(
        mongo_credentials, "enabled", lambda *_a: (_ for _ in ()).throw(AssertionError("admin gate must run first"))
    )
    monkeypatch.setattr(mongo_sync, "sync_profile", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("write")))
    reply = asyncio.run(_admin_runner(tmp_path)._handle_sync_command(_event(SUDO)))
    assert "Sync is owner/admin-only" in reply and "No data was written." in reply


def test_sync_status_failure_withholds_exception_details(tmp_path, monkeypatch):
    from agent import mongo_credentials
    from gateway import mongo_sync

    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: True)

    def fail(*_args, **_kw):
        raise RuntimeError("mongodb://private:password@example.invalid")

    monkeypatch.setattr(mongo_sync, "sync_profile", fail)
    reply = asyncio.run(_admin_runner(tmp_path)._handle_sync_command(_event(OWNER)))
    assert "Sync failed" in reply and "no data was written" in reply
    assert "password" not in reply and "private" not in reply


def test_sync_requires_authenticated_identity(tmp_path):
    reply = asyncio.run(runner_for(tmp_path)._handle_sync_command(SimpleNamespace(source=None)))
    assert "no authenticated source identity" in reply
