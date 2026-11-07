"""Last-used-route cache for the dangling-route rescue + ``fallback.auto_fallback``.

The rescue fires when the selected provider holds no credentials but is
still the route. It should prefer the route that just served a successful
turn over the first working provider's default model, and
``fallback.auto_fallback: false`` should disable the automatic rescue
without touching the explicit ``fallback_providers`` chain.

These tests pin:
  1. A cached (provider, model) pair wins over the first working provider's
     default model.
  2. With ``fallback.auto_fallback: false`` in a temp config.yaml, an
     exhausted chain on a credential-less route returns False (the turn
     fails instead of switching), while the explicit fallback chain still
     activates.
  3. Only completed turns record a route: a failed turn leaves no cache,
     and ``auto``/empty routes are never recorded.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_constants import get_hermes_home
from run_agent import AIAgent
from agent.error_classifier import FailoverReason
from agent.last_used_route import read_last_used_route, record_last_used_route


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
    """Patch the rescue's credential checks + client resolution.

    Only ``openai`` holds credentials; the first working provider would be
    ``gemini`` on its default model — so a rescue landing on OpenAI proves
    the cache won, not the default walk.
    """

    def _has_credentials(slug):
        return slug == "openai"

    patches = [
        patch(
            "agent.chat_completion_helpers._provider_has_hermes_credentials",
            side_effect=_has_credentials,
        ),
        patch(
            "agent.chat_completion_helpers._first_working_provider",
            return_value=("gemini", "gemini-2.5-flash"),
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o-mini"),
        ),
    ]
    for p in patches:
        p.start()
    agent._rescue_patches = patches
    return patches


def _stop_rescue_mocks(agent):
    for p in getattr(agent, "_rescue_patches", []):
        p.stop()


class TestLastUsedRouteRescue:
    def test_rescue_prefers_cached_last_used_route_over_first_working_provider(self):
        assert record_last_used_route(SimpleNamespace(provider="openai", model="gpt-4o-mini")) is True
        assert read_last_used_route() == ("openai", "gpt-4o-mini")
        agent = _make_agent()
        _rescue_mocks(agent)
        try:
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o-mini"
        assert agent._dangling_route_rescue == {
            "from_provider": "openai-codex", "to_provider": "openai", "to_model": "gpt-4o-mini",
        }

    def test_auto_fallback_off_disables_rescue_but_explicit_chain_still_works(self):
        home = get_hermes_home()
        (home / "config.yaml").write_text(
            "fallback:\n  auto_fallback: false\n", encoding="utf-8",
        )
        agent = _make_agent()
        _rescue_mocks(agent)
        try:
            # Automatic rescue disabled: the exhausted chain fails the turn.
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is False
            assert agent.provider == "openai-codex"
            assert getattr(agent, "_dangling_route_rescue", None) is None
            # The explicit chain is unaffected by the toggle.
            agent._fallback_chain = [{"provider": "openai", "model": "gpt-4o"}]
            agent._fallback_index = 0
            assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True
        finally:
            _stop_rescue_mocks(agent)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o"


class TestLastUsedRouteRecording:
    def test_only_completed_turns_record_a_route(self):
        from agent.conversation_loop import _record_last_used_route

        agent = SimpleNamespace(provider="openai", model="gpt-4o-mini")
        _record_last_used_route(agent, {"completed": False, "final_response": "boom"})
        assert read_last_used_route() is None
        _record_last_used_route(agent, {"completed": True, "final_response": "done"})
        assert read_last_used_route() == ("openai", "gpt-4o-mini")

    def test_auto_and_empty_routes_are_never_recorded(self):
        assert record_last_used_route(SimpleNamespace(provider="auto", model="gpt-4o")) is False
        assert record_last_used_route(SimpleNamespace(provider="openai", model="")) is False
        assert record_last_used_route(SimpleNamespace(provider="", model="gpt-4o")) is False
        assert read_last_used_route() is None
