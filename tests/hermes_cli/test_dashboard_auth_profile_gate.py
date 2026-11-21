"""Profile-scoped opt-in for the existing dashboard authentication gate."""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import yaml

from hermes_cli import web_server
from hermes_cli.config_defaults import DEFAULT_CONFIG


def _write_profile(home, *, require_auth=None):
    home.mkdir(parents=True)
    config = {}
    if require_auth is not None:
        config["dashboard"] = {"require_auth_on_loopback": require_auth}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _set_home(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_dashboard_loopback_gate_reads_active_profile_a_b_a(tmp_path, monkeypatch):
    """The effective config reader follows profile homes and absent means legacy-off."""
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    _write_profile(profile_a, require_auth=True)
    _write_profile(profile_b)  # no key: preserve existing local-only behavior
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr(web_server, "_dashboard_public_hosts", lambda: frozenset())

    assert DEFAULT_CONFIG["dashboard"]["require_auth_on_loopback"] is False
    from agent import secret_scope
    from gateway.run import _profile_runtime_scope
    launch = tmp_path / "launch"
    _write_profile(launch, require_auth=False)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    for home, expected in ((profile_a, True), (profile_b, False), (profile_a, True)):
        with _profile_runtime_scope(home):
            assert web_server.should_require_dashboard_auth("127.0.0.1") is expected
    assert __import__("os").environ["HERMES_HOME"] == str(launch)


def test_loopback_gate_config_validation_and_public_bind_fail_closed(tmp_path, monkeypatch):
    invalid_profile = tmp_path / "invalid-profile"
    public_profile = tmp_path / "public-profile"
    _write_profile(invalid_profile, require_auth=1)  # YAML integer is not a boolean
    _write_profile(public_profile, require_auth=False)
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setattr(web_server, "_dashboard_public_hosts", lambda: frozenset())

    _set_home(monkeypatch, invalid_profile)
    assert web_server.should_require_dashboard_auth("127.0.0.1") is True

    _set_home(monkeypatch, public_profile)
    assert web_server.should_require_dashboard_auth("0.0.0.0") is True


def test_start_server_applies_profile_loopback_gate_with_provider(tmp_path, monkeypatch):
    """Exercise start_server's real gate resolution while keeping uvicorn offline."""
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    _write_profile(profile_a, require_auth=True)
    _write_profile(profile_b, require_auth=False)
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.setattr(web_server, "_dashboard_public_hosts", lambda: frozenset())
    monkeypatch.setattr(web_server, "_port_bind_conflict", lambda host, port: False)
    monkeypatch.setattr(web_server, "_run_serve", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(
            Config=lambda *args, **kwargs: SimpleNamespace(loaded=True),
            Server=lambda config: SimpleNamespace(),
        ),
    )
    # _build_uvicorn_server also runs _uvicorn_console_logging(), which imports
    # uvicorn.config.LOGGING_CONFIG and uvicorn.logging formatters.
    monkeypatch.setitem(
        sys.modules,
        "uvicorn.config",
        SimpleNamespace(LOGGING_CONFIG={"formatters": {"default": {"fmt": "%(message)s"},
                                                        "access": {"fmt": "%(message)s"}}}),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn.logging",
        SimpleNamespace(AccessFormatter=logging.Formatter, DefaultFormatter=logging.Formatter),
    )
    # start_server imports these helpers locally; suppress unrelated process setup.
    import hermes_cli.nous_auth_keepalive as keepalive
    import tui_gateway.server as tui_server
    monkeypatch.setattr(keepalive, "start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr(tui_server, "install_exit_flush_signal_handlers", lambda: None)
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)

    for name in ("auth_required", "trusted_public_hosts", "bound_host", "initial_profile"):
        monkeypatch.setattr(web_server.app.state, name, getattr(web_server.app.state, name, None), raising=False)

    clear_providers()
    register_provider(StubAuthProvider())
    try:
        for home, expected in ((profile_a, True), (profile_b, False), (profile_a, True)):
            _set_home(monkeypatch, home)
            web_server.start_server(host="127.0.0.1", port=9119, open_browser=False)
            assert web_server.app.state.auth_required is expected

        # Desktop-owned loopback backends keep their per-spawn-token exemption,
        # even if the profile has explicitly opted into loopback auth.
        _set_home(monkeypatch, profile_a)
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-session-token")
        web_server.start_server(host="127.0.0.1", port=9119, open_browser=False)
        assert web_server.app.state.auth_required is False
    finally:
        clear_providers()
