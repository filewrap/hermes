"""Unit tests for the AgentRouter provider profile.

AgentRouter (agentrouter.org) is a unified LLM gateway whose OpenAI-compatible
surface sits at https://agentrouter.org/v1. Two quirks are load-bearing and
both were verified live 2026-11-21:

- WAF gate: every request 401s unless ``User-Agent`` is exactly
  ``claude-cli/2.0.0 (external, cli)``. The profile bakes that UA into
  ``default_headers`` so the turn client, the aux client and the live model
  fetch all send it.
- Wire: the served models (``gpt-5.6-sol``, ``gpt-6.1-sol``) 400 on
  ``/v1/chat/completions`` and work on ``/v1/responses``. The base
  ``api_mode_for_model`` hook returns None, and the resolver's fallback
  heuristic only matches ``gpt-5*`` — so without the profile override
  ``gpt-6.1-sol`` would stay on chat completions and 400.

These tests pin the profile's routing and wire-shape contract so a
refactor cannot silently drop the UA or the Responses routing.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

EXPECTED_USER_AGENT = "claude-cli/2.0.0 (external, cli)"


@pytest.fixture
def agentrouter_profile():
    """Resolve the registered AgentRouter profile through the real discovery path."""
    # ``model_tools`` triggers plugin discovery on import, which is what
    # registers the AgentRouter profile in the global provider registry.
    import model_tools
    import providers

    profile = providers.get_provider_profile("agentrouter")
    assert profile is not None, "agentrouter provider profile must be registered"
    return profile


class TestAgentRouterRegistration:
    """The profile is registered with the declaration the routes depend on."""

    def test_declares_responses_wire_and_endpoint(self, agentrouter_profile):
        assert agentrouter_profile.name == "agentrouter"
        assert agentrouter_profile.api_mode == "codex_responses"
        assert agentrouter_profile.base_url == "https://agentrouter.org/v1"
        assert "AGENTROUTER_API_KEY" in agentrouter_profile.env_vars

    def test_fallback_lists_the_live_models(self, agentrouter_profile):
        """Fallback/static lists must at least contain the ids verified live
        on 2026-11-21; the live fetch is what tracks later drift."""
        live = {"gpt-6.1-sol", "gpt-5.6-sol"}
        assert live <= set(agentrouter_profile.fallback_models)
        assert live <= set(agentrouter_profile.static_models)

    def test_default_headers_carry_the_exact_waf_user_agent(self, agentrouter_profile):
        assert agentrouter_profile.default_headers == {
            "User-Agent": EXPECTED_USER_AGENT
        }

    def test_alias_resolves_to_the_same_profile(self):
        import model_tools  # noqa: F401  (triggers discovery)
        import providers

        assert providers.get_provider_profile("agent-router") is not None


class TestAgentRouterResponsesRouting:
    """Both live models — and future ids — route through the Responses API."""

    @pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-5.6-sol", "gpt-7-future"])
    def test_api_mode_for_model_is_responses(self, agentrouter_profile, model):
        assert agentrouter_profile.api_mode_for_model(model) == "codex_responses"

    @pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-5.6-sol"])
    def test_requires_responses_api(self, model):
        """The gpt-6 case is the gap: the base heuristic only matches gpt-5*,
        so without the profile override gpt-6.1-sol would stay on chat
        completions and 400."""
        from run_agent import AIAgent

        assert AIAgent._provider_model_requires_responses_api(
            model, provider="agentrouter"
        ) is True

    def test_determine_api_mode_is_responses(self):
        """Robust in both environments: the host mandate is pure hostname
        matching (no I/O), checked before the models.dev pdef — so this holds
        live, where models.dev stands in with a generic openai_chat pdef,
        and under the test network fence alike."""
        from hermes_cli.providers import determine_api_mode

        assert (
            determine_api_mode(
                "agentrouter", "https://agentrouter.org/v1", model="gpt-6.1-sol"
            )
            == "codex_responses"
        )

    def test_host_mandates_responses(self):
        """The mandate entries (providers.py + runtime_provider.py mirror)
        pin the wire for the host itself."""
        from hermes_cli.providers import host_mandated_api_mode
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        assert (
            host_mandated_api_mode("https://agentrouter.org/v1")
            == "codex_responses"
        )
        assert (
            _detect_api_mode_for_url("https://agentrouter.org/v1")
            == "codex_responses"
        )

    def test_provider_def_resolves_to_the_endpoint(self):
        """The pdef resolves to the AgentRouter endpoint. Its ``transport``
        is deliberately NOT pinned: models.dev carries an AgentRouter entry,
        so live it returns the generic ``openai_chat`` pdef instead of the
        plugin-profile rung — the wire is decided by the host mandate, not
        by this field."""
        from hermes_cli.providers import get_provider

        pdef = get_provider("agentrouter")
        assert pdef is not None
        assert pdef.base_url == "https://agentrouter.org/v1"

    def test_host_maps_to_the_profile(self, agentrouter_profile):
        import providers

        assert providers.profile_for_host("https://agentrouter.org/v1") is agentrouter_profile


class _CaptureUAHandler(BaseHTTPRequestHandler):
    """Serves /models and records the User-Agent the fetch sent."""

    models = [{"id": "gpt-6.1-sol"}, {"id": "gpt-5.6-sol"}]
    seen_user_agent: str | None = None

    def do_GET(self):
        if self.path.rstrip("/") == "/models":
            type(self).seen_user_agent = self.headers.get("User-Agent")
            body = json.dumps({"data": self.models}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - stdlib name
        pass  # suppress noise


class TestAgentRouterFetchModels:
    """The live catalog fetch sends the exact WAF User-Agent."""

    def test_fetch_models_sends_exact_user_agent(self, agentrouter_profile):
        server = HTTPServer(("127.0.0.1", 0), _CaptureUAHandler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = agentrouter_profile.fetch_models(
                api_key="dummy-key-for-test",
                base_url=f"http://127.0.0.1:{port}",
            )
            assert result == ["gpt-6.1-sol", "gpt-5.6-sol"]
            assert _CaptureUAHandler.seen_user_agent == EXPECTED_USER_AGENT
        finally:
            server.shutdown()
