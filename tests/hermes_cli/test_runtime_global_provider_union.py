"""Runtime provider resolution must see globally-defined providers.

The model picker unions the launch home's global provider store into what it
shows (gateway/slash_commands_model.py), but the runtime resolved a profile's
``model.provider`` from the profile config only — so a group profile pointing
at a provider defined ONLY globally died with ``AuthError: Unknown provider``
before any request reached the endpoint. The same shape hit the picker's
``/models`` probe: it reads the key from the routed profile's secret scope,
but a globally-defined provider's key lives in the launch home's ``.env``,
so the probe went out unauthenticated and got 401.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent.runtime_scope import bind_profile_scope
from agent.secret_scope import is_multiplex_active, set_multiplex_active

LAUNCH_KEY = "test-launch-key"
PROFILE_KEY = "test-profile-key"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Launch home + one group profile home, wired as the process sees them."""
    launch = tmp_path / ".hermes"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profile = launch / "profiles" / "gc-1002397157264"
    profile.mkdir(parents=True)
    return launch, profile


@pytest.fixture
def multiplex_on():
    prev = is_multiplex_active()
    set_multiplex_active(True)
    try:
        yield
    finally:
        set_multiplex_active(prev)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _global_zcode(launch: Path, *, base_url: str, key_env: str | None = None,
                  api_key: str | None = None) -> None:
    lines = ["providers:", "  zcode-start:", f"    base_url: {base_url}"]
    if key_env:
        lines.append(f"    key_env: {key_env}")
    if api_key:
        lines.append(f"    api_key: {api_key}")
    lines.append("    model: glm-5.3")
    _write(launch / "config.yaml", "\n".join(lines) + "\n")


def _group_profile(profile: Path) -> None:
    _write(profile / "config.yaml", "model:\n  provider: zcode-start\n  default: glm-5.3\n")


def _authed_models_server():
    """Loopback /v1/models that 401s without the launch key, 200s with it.

    Returns (server, thread, seen) where seen collects (path, authorization)
    for every request, so the test asserts what the probe actually sent.
    """
    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            auth = self.headers.get("Authorization", "")
            seen.append((self.path, auth))
            if auth == f"Bearer {LAUNCH_KEY}":
                body = json.dumps({"data": [{"id": "glm-5.3"}]}).encode()
                self.send_response(200)
            else:
                body = json.dumps({"error": {"message": "unauthorized"}}).encode()
                self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the server
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, seen


@pytest.fixture
def models_server():
    server, thread, seen = _authed_models_server()
    try:
        yield server, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_group_profile_resolves_global_only_provider(homes, multiplex_on):
    """A group profile whose model.provider names a provider defined ONLY in
    the global store must resolve to that endpoint, not throw Unknown provider."""
    from hermes_cli.auth import AuthError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    launch, profile = homes
    _global_zcode(launch, base_url="http://127.0.0.1:8080", api_key="test-zcode-key")
    _group_profile(profile)

    with bind_profile_scope(profile):
        try:
            runtime = resolve_runtime_provider()
        except AuthError as exc:
            pytest.fail(f"globally-defined provider threw instead of resolving: {exc}")

    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "http://127.0.0.1:8080"


def test_global_definition_wins_on_slug_conflict(homes, multiplex_on):
    """The picker's rule is global-wins: when profile and global both define
    the slug, the runtime must take the global endpoint."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    launch, profile = homes
    _global_zcode(launch, base_url="http://127.0.0.1:8080", api_key="test-zcode-key")
    _write(profile / "config.yaml",
           "model:\n  provider: zcode-start\n  default: glm-5.3\n"
           "providers:\n  zcode-start:\n    base_url: http://127.0.0.1:9999\n"
           "    api_key: test-profile-key\n    model: glm-5.3\n")

    with bind_profile_scope(profile):
        runtime = resolve_runtime_provider()

    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "http://127.0.0.1:8080"


def test_profile_local_provider_still_resolves(homes, multiplex_on):
    """A provider defined only in the profile must keep resolving unchanged."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    launch, profile = homes
    _write(launch / "config.yaml", "model:\n  provider: openrouter\n")
    _write(profile / "config.yaml",
           "model:\n  provider: local-thing\n  default: local-model\n"
           "providers:\n  local-thing:\n    base_url: http://127.0.0.1:7777\n"
           "    api_key: test-profile-key\n    model: local-model\n")

    with bind_profile_scope(profile):
        runtime = resolve_runtime_provider()

    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "http://127.0.0.1:7777"


def test_probe_authenticates_with_launch_home_key(homes, multiplex_on, models_server, monkeypatch):
    """The /models probe for a globally-defined provider must authenticate
    with the key that lives only in the launch home's .env."""
    import hermes_cli.providers as providers_mod
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    monkeypatch.setattr(providers_mod, "HERMES_OVERLAYS", {})
    server, seen = models_server
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    launch, profile = homes
    _global_zcode(launch, base_url=base_url, key_env="ZCODE_LOCAL_API_KEY")
    _write(launch / ".env", f"ZCODE_LOCAL_API_KEY={LAUNCH_KEY}\n")
    _group_profile(profile)

    with bind_profile_scope(profile):
        rows = list_authenticated_providers(
            current_provider="zcode-start", current_base_url=base_url,
            current_model="glm-5.3",
            user_providers={"zcode-start": {
                "base_url": base_url, "key_env": "ZCODE_LOCAL_API_KEY", "model": "glm-5.3"}},
            custom_providers=[],
            probe_custom_providers=True,
        )

    row = next((r for r in rows if r["slug"] == "zcode-start"), None)
    assert row is not None, "globally-defined provider row missing from the picker"
    assert row["models"] == ["glm-5.3"], "probe did not authenticate: models list is empty"
    assert any(auth == f"Bearer {LAUNCH_KEY}" for _, auth in seen), (
        "probe never sent the launch-home key")


def test_probe_does_not_borrow_launch_key_for_profile_local_provider(
        homes, multiplex_on, models_server, monkeypatch):
    """Fail-closed side: a profile-local provider's key_env must never resolve
    from the launch home's .env, even when the same variable exists there."""
    import hermes_cli.providers as providers_mod
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    monkeypatch.setattr(providers_mod, "HERMES_OVERLAYS", {})
    server, seen = models_server
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    launch, profile = homes
    _write(launch / ".env", f"LOCAL_THING_KEY={LAUNCH_KEY}\n")
    _write(profile / "config.yaml",
           "model:\n  provider: local-thing\n  default: local-model\n")

    with bind_profile_scope(profile):
        rows = list_authenticated_providers(
            current_provider="local-thing", current_base_url=base_url,
            current_model="local-model",
            user_providers={"local-thing": {
                "base_url": base_url, "key_env": "LOCAL_THING_KEY", "model": "local-model"}},
            custom_providers=[],
            probe_custom_providers=True,
        )

    row = next((r for r in rows if r["slug"] == "local-thing"), None)
    assert row is not None, "profile-local provider row missing from the picker"
    assert not any(auth == f"Bearer {LAUNCH_KEY}" for _, auth in seen), (
        "probe borrowed the launch home's key for a profile-local provider")


def test_turn_time_resolves_key_env_from_launch_home_env(homes, multiplex_on):
    """A globally-defined provider's key_env, present only in the launch
    home's .env, must resolve at turn time — the live zcode-start case: the
    group profile selected it and the turn went out with the no-key-required
    placeholder and 401d."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    launch, profile = homes
    _global_zcode(launch, base_url="http://127.0.0.1:8080", key_env="ZCODE_LOCAL_API_KEY")
    _write(launch / ".env", f"ZCODE_LOCAL_API_KEY={LAUNCH_KEY}\n")
    _group_profile(profile)

    with bind_profile_scope(profile):
        runtime = resolve_runtime_provider()

    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "http://127.0.0.1:8080"
    assert runtime["api_key"] == LAUNCH_KEY


def test_turn_time_profile_key_env_wins_over_launch_home(homes, multiplex_on):
    """When the profile defines the same key_env variable in its own .env,
    the profile's value wins over the launch home's."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    launch, profile = homes
    _global_zcode(launch, base_url="http://127.0.0.1:8080", key_env="ZCODE_LOCAL_API_KEY")
    _write(launch / ".env", f"ZCODE_LOCAL_API_KEY={LAUNCH_KEY}\n")
    _write(profile / ".env", f"ZCODE_LOCAL_API_KEY={PROFILE_KEY}\n")
    _group_profile(profile)

    with bind_profile_scope(profile):
        runtime = resolve_runtime_provider()

    assert runtime["api_key"] == PROFILE_KEY


def test_turn_time_inline_secret_ref_resolves_from_launch_home(homes, multiplex_on):
    """A globally-defined provider whose api_key is a $VAR ref, with the
    variable only in the launch home's .env, must resolve at turn time."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    launch, profile = homes
    _global_zcode(launch, base_url="http://127.0.0.1:8080", api_key="$ZCODE_LOCAL_API_KEY")
    _write(launch / ".env", f"ZCODE_LOCAL_API_KEY={LAUNCH_KEY}\n")
    _group_profile(profile)

    with bind_profile_scope(profile):
        runtime = resolve_runtime_provider()

    assert runtime["api_key"] == LAUNCH_KEY
