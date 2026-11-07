"""Setup offers every registered platform.

The telegram-only gate (``GATEWAY_ADAPTER_ALLOWLIST``) was removed 2026-11-01
("let adaptor show them"), so a registered plugin platform is offered by the
setup menu instead of hidden. These tests pin that.
"""

from gateway.platform_registry import PlatformEntry, platform_registry


def _register_irc_platform():
    platform_registry.register(PlatformEntry(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        required_env=["IRC_SERVER"],
        setup_fn=lambda: None,
        source="plugin",
        plugin_name="irc_platform",
        emoji="💬",

    ))


def test_registered_plugin_platform_is_offered(monkeypatch):
    import hermes_cli.gateway as gateway_mod

    _register_irc_platform()
    try:
        platforms = gateway_mod._all_platforms()
        keys = {platform["key"] for platform in platforms}
        assert "telegram" in keys
        assert "irc" in keys
    finally:
        platform_registry.unregister("irc")


def test_setup_gateway_checklist_contains_telegram_and_irc(monkeypatch, tmp_path):
    import hermes_cli.gateway as gateway_mod
    from hermes_cli import gateway_setup_service, setup as setup_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    _register_irc_platform()
    checklist_calls = []
    try:
        monkeypatch.setattr(
            setup_mod, "prompt_checklist",
            lambda title, choices, pre_selected=None: checklist_calls.append(list(choices)) or [],
        )
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **kw: False)
        monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
        monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)
        monkeypatch.setattr(gateway_setup_service, "ensure_gateway_service", lambda **kw: None)

        setup_mod.setup_gateway({})

        assert checklist_calls
        choices = "\n".join(checklist_calls[0])
        assert "Telegram" in choices
        assert "IRC" in choices
    finally:
        platform_registry.unregister("irc")
