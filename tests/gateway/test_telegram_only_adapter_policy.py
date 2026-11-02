"""The gateway exposes adapters for every configured platform, like upstream.

No Telegram-only adapter policy: the factory creates adapters for any
platform, the CLI send path routes to any configured platform, and the
dashboard surfaces (webhooks, cron delivery, pairing, onboarding) accept
non-Telegram platforms. Upstream has no GATEWAY_ADAPTER_ALLOWLIST.
"""

import asyncio
import json
import time
from argparse import Namespace
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run_adapters import GatewayAdapterLifecycleMixin


@pytest.fixture()
def isolated_profiles(tmp_path, monkeypatch):
    """Isolated default home with one named profile, as in
    tests/hermes_cli/test_web_server_cron_profiles.py."""
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"

    for home in (default_home, worker_home):
        (home / "cron").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home}


def test_adapter_factory_creates_non_telegram_adapter(monkeypatch):
    runner = object.__new__(GatewayAdapterLifecycleMixin)
    adapter = SimpleNamespace()
    called = []

    def _instantiate(self, platform, config):
        called.append(platform)
        return adapter

    monkeypatch.setattr(GatewayAdapterLifecycleMixin, "_instantiate_adapter", _instantiate)

    assert runner._create_adapter(Platform.DISCORD, PlatformConfig(enabled=True)) is adapter
    assert called == [Platform.DISCORD]
    assert adapter.gateway_runner is runner


def test_adapter_factory_still_allows_telegram(monkeypatch):
    runner = object.__new__(GatewayAdapterLifecycleMixin)
    adapter = SimpleNamespace()
    monkeypatch.setattr(GatewayAdapterLifecycleMixin, "_instantiate_adapter", lambda self, platform, config: adapter)

    assert runner._create_adapter(Platform.TELEGRAM, PlatformConfig(enabled=True)) is adapter
    assert adapter.gateway_runner is runner


def test_dashboard_status_projection_exposes_all_platform_keys():
    from hermes_cli.web_routers.status import _status_platform_key_allowed

    assert _status_platform_key_allowed("telegram", None)
    assert _status_platform_key_allowed("worker_alpha:telegram", None)
    assert _status_platform_key_allowed("discord", None)
    assert _status_platform_key_allowed("worker_alpha:discord", None)


def test_cli_send_routes_non_telegram_target_to_send_tool(monkeypatch):
    """``hermes send`` hands a discord target to the send tool instead of
    refusing it at a policy gate (upstream ``cmd_send`` has no platform gate)."""
    from hermes_cli import send_cmd
    from tools import send_message_tool as smt

    monkeypatch.setattr(send_cmd, "_load_hermes_env", lambda: None)
    seen = {}

    def _fake_tool(args):
        seen.update(args)
        return json.dumps({"success": True, "note": "sent"})

    monkeypatch.setattr(smt, "send_message_tool", _fake_tool)

    with pytest.raises(SystemExit) as exc:
        send_cmd.cmd_send(Namespace(list_targets=False, to="discord:#ops", message="hello"))
    assert exc.value.code == 0
    assert seen == {"action": "send", "target": "discord:#ops", "message": "hello"}


def test_shared_send_tool_reports_unconfigured_non_telegram_target():
    """A discord send reaches the platform-config stage: unconfigured here,
    so the failure is the config error, not a Telegram-only refusal."""
    from tools.send_message_tool import send_message_tool

    result = json.loads(send_message_tool({"action": "send", "target": "discord", "message": "hello"}))
    assert "Platform 'discord' is not configured" in result["error"]
    assert "Only Telegram is enabled" not in result["error"]


def test_dashboard_onboarding_and_pairing_accept_non_telegram(monkeypatch):
    """WhatsApp onboarding runs its session logic and discord pairing reaches
    the store lookup — neither surface refuses non-Telegram platforms."""
    import hermes_cli.config as cfg_mod
    from hermes_cli import web_models
    import hermes_cli.web_routers.messaging as rt_messaging
    from hermes_cli import web_server_messaging
    from fastapi import HTTPException
    from hermes_cli.web_models import PairingApprove
    from hermes_cli.web_routers.ops import approve_pairing

    saved = {}
    removed = []
    enabled = []
    monkeypatch.setattr(cfg_mod, "save_env_value", lambda key, value: saved.setdefault(key, value))
    monkeypatch.setattr(cfg_mod, "remove_env_value", lambda key: removed.append(key))
    monkeypatch.setattr(web_server_messaging, "_write_platform_enabled", lambda platform, value: enabled.append((platform, value)))
    monkeypatch.setattr(
        web_server_messaging,
        "_restart_gateway_after_whatsapp_onboarding",
        lambda profile=None: {"restart_started": True, "restart_pid": 12345},
    )
    record = web_server_messaging._WhatsAppOnboardingSession(
        proc=None,
        mode="bot",
        allowed_users="",
        session_path="/tmp/session",
        expires_at="2099-01-01T00:00:00Z",
        expires_at_ts=time.time() + 600,
        status="connected",
    )
    web_server_messaging._whatsapp_onboarding_sessions.clear()
    web_server_messaging._whatsapp_onboarding_sessions["pairing"] = record

    result = asyncio.run(
        rt_messaging.apply_whatsapp_onboarding(
            "pairing",
            web_models.WhatsAppOnboardingApply(mode="bot", allowed_users=""),
        )
    )
    assert result["ok"] is True
    assert saved["WHATSAPP_MODE"] == "bot"
    assert saved["WHATSAPP_DM_POLICY"] == "pairing"
    assert enabled == [("whatsapp", True)]

    # No discord pairing request exists, so the store answers 404 — a lookup
    # miss, not a policy refusal.
    with pytest.raises(HTTPException) as pairing_error:
        asyncio.run(approve_pairing(PairingApprove(platform="discord", request_id="no-such-request")))
    assert pairing_error.value.status_code == 404
    assert "not found or expired" in pairing_error.value.detail


def test_webhook_and_dashboard_cron_delivery_targets_are_not_telegram_only(monkeypatch, tmp_path):
    """Webhook delivery accepts any platform target; the receiver gate is the
    webhook platform's own enabled flag; the dashboard cron update path
    accepts a non-Telegram delivery target."""
    from cron.scheduler_delivery import _is_known_delivery_platform
    from hermes_cli.web_routers.cron import _normalize_dashboard_cron_updates
    from hermes_cli.webhook import webhook_delivery_target_allowed, webhook_receiver_allowed
    import hermes_cli.webhook as wh

    assert _is_known_delivery_platform("telegram")
    # Registry-based, not policy: discord/email declare a cron home-channel env
    # var, so the resolver knows them.
    assert _is_known_delivery_platform("discord")
    assert _is_known_delivery_platform("email")
    assert webhook_delivery_target_allowed("telegram")
    assert webhook_delivery_target_allowed("log")
    assert webhook_delivery_target_allowed("github_comment")
    assert webhook_delivery_target_allowed("discord")
    assert webhook_delivery_target_allowed("email")
    # Disabled here (webhook platform not enabled), so the receiver is not allowed.
    assert not webhook_receiver_allowed()
    monkeypatch.setattr(wh, "_get_webhook_config", lambda: {"enabled": True})
    assert webhook_receiver_allowed()
    assert _normalize_dashboard_cron_updates({"deliver": "slack:#ops"}, tmp_path)["deliver"] == "slack:#ops"


def test_dashboard_webhook_creation_accepts_discord_destination(monkeypatch):
    import hermes_cli.webhook as wh
    from hermes_cli.web_models import WebhookCreate
    from hermes_cli.web_routers.ops import create_webhook

    monkeypatch.setattr(wh, "_is_webhook_enabled", lambda: True)

    summary = asyncio.run(create_webhook(WebhookCreate(name="allowed", deliver="discord")))
    assert summary["name"] == "allowed"
    assert summary["deliver"] == "discord"
    assert wh._load_subscriptions()["allowed"]["deliver"] == "discord"


def test_dashboard_webhook_receiver_can_be_enabled_and_create_routes():
    import hermes_cli.webhook as wh
    from hermes_cli.web_models import WebhookCreate
    from hermes_cli.web_routers.ops import create_webhook, enable_webhooks

    result = asyncio.run(enable_webhooks())
    assert result["ok"] is True
    assert result["platform"] == "webhook"
    assert result["enabled"] is True

    summary = asyncio.run(create_webhook(WebhookCreate(name="active", deliver="telegram")))
    assert summary["name"] == "active"
    assert summary["deliver"] == "telegram"
    assert wh._load_subscriptions()["active"]["deliver"] == "telegram"


def test_cron_resolver_drops_unknown_platform_targets(monkeypatch):
    """The resolver drops targets for platforms it knows nothing about (no
    registry entry, no home channel). Known platforms resolve even when they
    are not Telegram — the resolver is registry-based, not policy-based."""
    import cron.scheduler_delivery as delivery

    monkeypatch.setattr(
        delivery, "_resolve_origin",
        lambda job: {"platform": "nosuchplatform", "chat_id": "old-room", "thread_id": None},
    )
    assert delivery._resolve_single_delivery_target({}, "origin") is None
    assert delivery._resolve_single_delivery_target({}, "nosuchplatform:old-room") is None
    assert delivery._resolve_single_delivery_target({}, "nosuchplatform") is None

    monkeypatch.setattr(delivery, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(delivery, "_iter_home_target_platforms", lambda: ["nosuchplatform"])
    monkeypatch.setattr(delivery, "_get_home_target_chat_id", lambda platform: "old-room")
    assert delivery._resolve_single_delivery_target({}, "origin") is None


@pytest.mark.asyncio
async def test_dashboard_blueprint_accepts_non_telegram_delivery(isolated_profiles, monkeypatch):
    """Instantiating a blueprint with a slack delivery target creates the job
    (upstream ``instantiate_blueprint`` performs no delivery-target policy check)."""
    from hermes_cli import web_models
    import hermes_cli.web_routers.cron as rt_cron
    from hermes_cli import web_server_cron

    notified_profiles = []
    monkeypatch.setattr(
        web_server_cron,
        "_notify_cron_provider_for_profile",
        notified_profiles.append,
    )

    created = await rt_cron.instantiate_blueprint(
        web_models.AutomationBlueprintInstantiate(
            blueprint="morning-brief", values={"time": "07:30", "deliver": "slack"}
        ),
        profile="worker_alpha",
    )

    assert created["profile"] == "worker_alpha"
    assert created["deliver"] == "slack"
    assert notified_profiles == ["worker_alpha"]
