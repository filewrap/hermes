"""One-time notified auto-fallback for a removed-but-still-selected provider.

When the selected provider's credentials were removed but the route still
points at it (removal failed to clear the route), the turn fails on the dead
provider even though a working provider exists. The rescue fires exactly
once, inside ``try_activate_fallback`` after the configured chain is
exhausted: it installs the first other authenticated provider as a one-entry
chain, marks the turn result so the gateway can prompt the user to switch
permanently, and never fires again for the same agent.

These tests pin:
  1. The rescue activates when the current provider has no Hermes credentials
     and a working provider exists (provider/model swap in place).
  2. It does NOT fire when the current provider still has credentials
     (ordinary rate-limit: the normal exhausted path applies).
  3. It does NOT fire when no working provider exists.
  4. It fires only once per agent lifetime.
"""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from agent.error_classifier import FailoverReason


def _make_agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=None,
        )
        agent.client = MagicMock()
        agent.provider = "openai-codex"
        agent.model = "gpt-5.6-luna-900k"
        return agent


def _mock_client(base_url="https://api.openai.com/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


def _rescue_mocks(agent):
    """Patch the rescue's credential checks + client resolution for a working provider."""
    patches = [
        patch(
            "agent.chat_completion_helpers._provider_has_hermes_credentials",
            return_value=False,
        ),
        patch(
            "agent.chat_completion_helpers._first_working_provider",
            return_value=("openai", "gpt-4o"),
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ),
    ]
    for p in patches:
        p.start()
    agent._rescue_patches = patches
    return patches


def _stop_rescue_mocks(agent):
    for p in getattr(agent, "_rescue_patches", []):
        p.stop()


class TestDanglingRouteRescue:
    def test_rescue_activates_when_provider_has_no_credentials(self):
        agent = _make_agent()
        _rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o"
        assert agent._dangling_route_rescue == {
            "from_provider": "openai-codex", "to_provider": "openai", "to_model": "gpt-4o",
        }
        assert agent._dangling_route_rescue_attempted is True

    def test_rescue_does_not_fire_when_provider_still_has_credentials(self):
        agent = _make_agent()
        with (
            patch(
                "agent.chat_completion_helpers._provider_has_hermes_credentials",
                return_value=True,
            ),
            patch(
                "agent.chat_completion_helpers._first_working_provider",
                return_value=("openai", "gpt-4o"),
            ),
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False
        assert agent.provider == "openai-codex"
        assert getattr(agent, "_dangling_route_rescue", None) is None

    def test_rescue_does_not_fire_without_working_provider(self):
        agent = _make_agent()
        with (
            patch(
                "agent.chat_completion_helpers._provider_has_hermes_credentials",
                return_value=False,
            ),
            patch(
                "agent.chat_completion_helpers._first_working_provider",
                return_value=None,
            ),
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False
        assert agent.provider == "openai-codex"
        assert getattr(agent, "_dangling_route_rescue", None) is None

    def test_rescue_fires_only_once(self):
        agent = _make_agent()
        _rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
            # The rescue chain is spent and the guard is set: a second failure
            # on the same dangling route is not rescued again.
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False
        finally:
            _stop_rescue_mocks(agent)
        assert agent._dangling_route_rescue_attempted is True
